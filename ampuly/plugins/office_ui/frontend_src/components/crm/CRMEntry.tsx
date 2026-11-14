import { useEffect, useRef, useState } from 'react'
import { crmClient, crmErrorMessage } from './client'
import type { CRMRequest, CRMSettings } from './client'
import { CRMWorkbench } from './CRMWorkbench'

export class CRMAckBus {
  private listeners = new Set<(ack: Record<string, unknown>) => void>()
  subscribe = (listener: (ack: Record<string, unknown>) => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  receive(ack: Record<string, unknown>): boolean {
    if (typeof ack.action !== 'string' || !ack.action.startsWith('crm_')) return false
    for (const listener of this.listeners) listener(ack)
    return true
  }
}

type Props = { bus: CRMAckBus; projectId: string; identity: string; connected: boolean; allowed: boolean;
  send: (message: Record<string, unknown>) => void; onBusyChange?: (busy: boolean) => void }

function ConnectedWorkbench({ bus, projectId, send, connected, onBusyChange }: Props) {
  const sendRef = useRef(send)
  sendRef.current = send
  const [request, setRequest] = useState<CRMRequest | null>(null)
  useEffect(() => {
    if (!connected) { setRequest(null); return }
    const client = crmClient(projectId, (message) => sendRef.current(message), bus.subscribe)
    setRequest(() => client.request)
    return () => { client.close(); setRequest(null) }
  }, [bus, projectId, connected])
  return request ? <CRMSettingsGate projectId={projectId} connected={connected} request={request} onBusyChange={onBusyChange} /> : <p role="status">正在连接销售工作台…</p>
}

export type GateProps = { projectId: string; connected: boolean; request: CRMRequest; onBusyChange?: (busy: boolean) => void }

// CRM is default OFF per project. The server refuses every data action until
// an owner enables it here; admission is synthetic-only and cannot be widened
// from the UI (real-data admission is closed server-side).
export function CRMSettingsGate({ projectId, connected, request, onBusyChange }: GateProps) {
  const [settings, setSettings] = useState<CRMSettings | null>(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    let live = true
    setSettings(null); setError('')
    request('crm_settings_get', {}).then((reply) => { if (live) setSettings(reply as unknown as CRMSettings) })
      .catch((reason: Error) => { if (live) setError(reason.message) })
    return () => { live = false }
  }, [request, projectId])
  async function change(enabled: boolean) {
    if (!settings || saving) return
    setSaving(true); setError('')
    try {
      const reply = await request('crm_settings_set', { enabled, admission: 'synthetic_only', expected_revision: settings.revision })
      setSettings(reply as unknown as CRMSettings)
    } catch (reason) { setError((reason as Error).message) } finally { setSaving(false) }
  }
  if (error && !settings) return <p role="alert" className="crm-settings">{crmErrorMessage(error)}</p>
  if (!settings) return <p role="status">正在读取销售记录设置…</p>
  if (!settings.enabled) {
    return <section className="crm-settings" aria-label="销售记录未启用">
      <p><strong>本项目未启用销售记录（默认关闭）。</strong></p>
      <p>启用后仅允许合成数据：邮箱和网站须使用 example.com、*.test、*.invalid 等保留域名，电话须使用 555-01xx 虚构号码或 TEST- 前缀。真实客户资料准入未开放。</p>
      {error && <p role="alert">{crmErrorMessage(error)}</p>}
      <button type="button" disabled={!connected || saving} onClick={() => { void change(true) }}>为本项目启用（仅合成数据）</button>
    </section>
  }
  return <>
    <p className="crm-settings" role="note">合成数据模式：真实客户资料准入未开放。{error && <span role="alert">{crmErrorMessage(error)}</span>}
      <button type="button" disabled={!connected || saving} onClick={() => { void change(false) }}>停用本项目销售记录</button></p>
    <CRMWorkbench projectId={projectId} connected={connected} request={request} onBusyChange={onBusyChange} />
  </>
}

// A leaf view inside the existing project's connections section, no app shell.
export function CRMProjectRecords(props: Props) {
  if (!props.allowed) return null
  return <ConnectedWorkbench key={`${props.projectId}:${props.identity}`} {...props} />
}
