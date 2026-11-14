import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { VisualSocketClient } from '../../lib/wsClient'
import type { ReworkInspectionBinding } from '../../lib/aiEmployeeReworkInspectionScope'
import { AIEmployeeReworkPendingController } from '../../lib/aiEmployeeReworkPendingController'

type Transport = Pick<VisualSocketClient, 'send' | 'addAckObserver'>
type Props = { binding: ReworkInspectionBinding | null; connected: boolean; transport: Transport | null }
const EMPTY = Object.freeze({ records: [], readState: 'idle' as const, writeState: 'idle' as const, checking: false, message: '' })
const empty = () => EMPTY, subscribeEmpty = () => () => undefined

/** Same order drawer and App-owned transport; no second navigation or socket. */
export function AIEmployeeReworkPendingHost({ binding, connected, transport }: Props) {
  const connectedRef = useRef(connected)
  connectedRef.current = connected
  const [owned, setOwned] = useState<{ controller: AIEmployeeReworkPendingController; revision: string; transport: Transport } | null>(null)
  const controller = owned && binding && owned.revision === binding.context_revision && owned.transport === transport ? owned.controller : null
  const snapshot = useSyncExternalStore(controller?.subscribe ?? subscribeEmpty, controller?.getSnapshot ?? empty, empty)
  useEffect(() => {
    if (!binding || !transport) { setOwned(null); return }
    const next = new AIEmployeeReworkPendingController({ scope: binding.scope,
      send: message => transport.send({ ...message }), connected: () => connectedRef.current,
      nextRequestId: () => `rework-${globalThis.crypto.randomUUID()}`,
      setTimer: (callback, ms) => window.setTimeout(callback, ms), clearTimer: timer => window.clearTimeout(timer as number) })
    let live = true
    let remove: () => void
    try { remove = transport.addAckObserver(payload => { if (live) next.receive(payload) }) }
    catch { next.dispose(); setOwned(null); return }
    setOwned({ controller: next, revision: binding.context_revision, transport })
    return () => { live = false; try { remove() } finally { next.dispose() } }
  }, [binding?.context_revision, transport])
  useEffect(() => { if (!connected) controller?.connectionLost() }, [connected, controller])
  if (!binding) return null
  const busy = snapshot.readState === 'pending' || snapshot.writeState === 'pending' || snapshot.checking
  return <section className="employee-rework-card employee-operations" data-testid="ai-employee-rework-pending">
    <h3>人工返修</h3>
    <button type="button" className="shell-btn" disabled={!controller || !connected || busy} onClick={() => controller?.inspect()}>
      {snapshot.readState === 'pending' ? '正在读取，等回执' : '读取待返修记录'}
    </button>
    {snapshot.message && <p role="status">{snapshot.message}</p>}
    {snapshot.records.map(record => <article key={record.record_id}>
      <p>审核节点：{String(record.request.review_node_id)} → 返修节点：{String(record.request.target_node_id)}</p>
      <p>第 {String(record.request.expected_attempt)} 轮 · 材料版本 {String(record.request.reviewed_artifact_version)}</p>
      <p style={{ whiteSpace: 'pre-wrap' }}>{String(record.request.revision_note)}</p>
      <p>{record.consequence}</p>
      <button type="button" className="shell-btn primary" data-action="human_review_rework_confirm"
        disabled={!controller || !connected || busy || ['unknown', 'confirmed'].includes(snapshot.writeState)
          || !record.allowed_actions.includes('human_review_rework_confirm')}
        onClick={() => controller?.confirm(record)}>确认从原节点返修</button>
    </article>)}
    {snapshot.writeState === 'unknown' && <button type="button" className="shell-btn" disabled={!controller || !connected || busy}
      data-action="human_review_rework_lookup" onClick={() => controller?.lookup()}>核对原确认结果</button>}
  </section>
}
