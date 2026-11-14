// The builder's form for a step's desktop connector (4.7-A06): the programs it may work in, what it has to get done,
// the done-when conditions code checks on the desktop, and which deliverable the result is written to. The canvas
// shows it in place of the browser form when the step's one `connector` field is marked `kind: 'desktop'`.
import { useSyncExternalStore } from 'react'
import {
  DESKTOP_CHANNEL_CLOSED_NOTE,
  DESKTOP_POSTCONDITION_KINDS,
  DESKTOP_POSTCONDITION_LABELS,
  appsFromText,
  appsText,
  desktopPostKey,
  type DesktopConnectorParams,
  type DesktopPostconditionKind,
} from '../workspace/desktopConnector'

/** Asks the console to open 配置 → 渠道连接 at the desktop channel's switch (App listens). An event rather than a
 *  callback: the canvas that holds this form is several pages deep, and the builder's drafts outlive leaving it. */
export const OPEN_DESKTOP_CHANNEL_EVENT = 'ampuly:open-desktop-channel'

/** Whether this console may open the desktop channel: the owner, decided as the server decides it (lib/ownerConsole).
 *  App says so as the sign-in changes; the form, several pages deep, reads it here rather than having it threaded
 *  through the canvas. Until App has said, nobody may -- a button the server would refuse is never shown. */
let channelOpener = false
const openerListeners = new Set<() => void>()

export function setDesktopChannelOpener(owner: boolean): void {
  if (owner === channelOpener) return
  channelOpener = owner
  openerListeners.forEach(listener => listener())
}

function subscribeOpener(listener: () => void): () => void {
  openerListeners.add(listener)
  return () => { openerListeners.delete(listener) }
}

function readOpener(): boolean {
  return channelOpener
}

export function DesktopConnectorFieldset({ nodeId, connector, deliverables, zh, channelOpen, canOpenChannel, onChange }: {
  nodeId: string
  connector: DesktopConnectorParams
  deliverables: readonly string[]
  zh: boolean
  /** Whether the owner opened the desktop channel (控制面 → 本机桌面通道); closed, the step saves but does not run. */
  channelOpen: boolean
  /** Whether this viewer may open it; left out, what App said (setDesktopChannelOpener). */
  canOpenChannel?: boolean
  /** The step's new connector field: these parameters, or null to take the connector off. */
  onChange: (connector: DesktopConnectorParams | null) => void
}) {
  const opener = useSyncExternalStore(subscribeOpener, readOpener, readOpener)
  const mayOpen = canOpenChannel ?? opener
  const set = (patch: Partial<DesktopConnectorParams>) => onChange({ ...connector, ...patch })
  const posts = connector.postconditions ?? []
  return (
    <fieldset className="wf-node-edit-connector" data-testid="node-connector-desktop">
      <legend>{zh ? '桌面应用连接器' : 'Desktop app connector'}</legend>
      <label className="wf-node-edit-toggle">
        <input type="checkbox" data-field="connector" checked onChange={event => { if (!event.target.checked) onChange(null) }} />
        <span>{zh ? '这一步要在这台电脑上装的程序里做（比如记事本、WPS 这类本机应用）'
                  : 'This step works in a program installed on this computer (such as Notepad or WPS)'}</span>
      </label>
      {!channelOpen && (
        <p className="wf-node-edit-hint" role="status" data-testid="node-connector-desktop-closed">{DESKTOP_CHANNEL_CLOSED_NOTE[zh ? 0 : 1]}
          {' '}{mayOpen
            ? <button type="button" className="settings-link-btn" data-testid="node-connector-desktop-open-channel"
                onClick={() => window.dispatchEvent(new CustomEvent(OPEN_DESKTOP_CHANNEL_EVENT))}>{zh ? '去打开' : 'Turn it on'}</button>
            : <span data-testid="node-connector-desktop-owner-only">{zh ? '只有负责人能打开' : 'Only the owner can turn it on'}</span>}</p>
      )}
      <label className="wf-node-edit-field">
        <span>{zh ? '允许的应用' : 'Allowed programs'}</span>
        <textarea key={`${nodeId}-apps`} data-field="connector.allowed_apps" rows={2}
          defaultValue={appsText(connector.allowed_apps)}
          placeholder={zh ? '每行一个程序名，如 notepad.exe' : 'one program per line, e.g. notepad.exe'}
          onBlur={event => set({ allowed_apps: appsFromText(event.target.value) })} />
      </label>
      <label className="wf-node-edit-field">
        <span>{zh ? '要做成什么' : 'Goal'}</span>
        <textarea key={`${nodeId}-desktop-goal`} data-field="connector.goal" rows={2} defaultValue={connector.goal}
          onBlur={event => set({ goal: event.target.value.trim() })} />
      </label>
      <div className="wf-node-edit-field" data-testid="node-connector-desktop-posts">
        <span>{zh ? '怎样算做完（由代码在桌面上核对）' : 'Done when (checked by code on the desktop)'}</span>
        {posts.map((post, index) => (
          <div className="wf-node-edit-post" key={`${nodeId}-desktop-post-${index}`}>
            <select data-field={`connector.postconditions.${index}.kind`} value={post.kind}
              onChange={event => set({ postconditions: posts.map((p, i) => i === index ? { ...p, kind: event.target.value as DesktopPostconditionKind } : p) })}>
              {DESKTOP_POSTCONDITION_KINDS.map(kind => <option key={kind} value={kind}>{DESKTOP_POSTCONDITION_LABELS[kind][zh ? 0 : 1]}</option>)}
            </select>
            <input data-field={`connector.postconditions.${index}.value`} defaultValue={post.value} key={desktopPostKey(nodeId, index, post)}
              placeholder={zh ? '如 已保存' : 'e.g. Saved'}
              onBlur={event => set({ postconditions: posts.map((p, i) => i === index ? { ...p, value: event.target.value.trim() } : p) })} />
            <button type="button" onClick={() => set({ postconditions: posts.filter((_, i) => i !== index) })}>{zh ? '删去' : 'Remove'}</button>
          </div>
        ))}
        <button type="button" data-testid="node-connector-desktop-add-post" data-field="connector.postconditions"
          onClick={() => set({ postconditions: [...posts, { kind: 'window_title_contains', value: '' }] })}>{zh ? '＋ 加一条' : '+ Add'}</button>
      </div>
      <label className="wf-node-edit-field">
        <span>{zh ? '结果写到' : 'Result goes to'}</span>
        <select data-field="connector.output.path" value={connector.output?.path ?? ''}
          onChange={event => set({ output: { ...(connector.output ?? {}), path: event.target.value } })}>
          <option value="">{zh ? '（选这一步的一项交付）' : '(pick one of the step\'s deliverables)'}</option>
          {deliverables.map(path => <option key={path} value={path}>{path}</option>)}
        </select>
      </label>
      <small data-testid="node-connector-desktop-note">{zh
        ? '只在列出的程序里操作；不能启动程序、不碰剪贴板和文件对话框；回车、快捷键这类会提交的按键会被拦下，交给人。结果不明的操作不会自动重做，要等人看过。'
        : 'Works only in the listed programs; never starts a program, uses the clipboard or a file dialog; keys that submit are stopped and handed to a person. An action whose outcome is unknown is never redone on its own: a person looks first.'}</small>
    </fieldset>
  )
}
