import { useEffect, useRef, useState } from 'react'

type Props = { projectId: string; taskId: string; authToken: string; zh: boolean; compact?: boolean }

/** Original observations only. No remote input, automatic grant or screen capture. */
export function DesktopReadonlyView({ projectId, taskId, authToken, zh, compact }: Props) {
  const [image, setImage] = useState<string | null>(null)
  const [active, setActive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [at, setAt] = useState<string | null>(null)
  const state = useRef({ generation: 0, view: '', url: '', timer: 0, imageTimer: 0, expires: 0,
    abort: null as AbortController | null })
  const endpoint = `/api/desktop-view/${encodeURIComponent(projectId)}/${encodeURIComponent(taskId)}`
  const headers = (view = '') => ({ Authorization: `Bearer ${authToken}`,
    'X-Ampuly-Desktop-View': '1', ...(view ? { 'X-Ampuly-Desktop-View-Id': view } : {}) })

  const closeView = (view: string) => {
    // Capture this render's OLD endpoint/token; never revoke a replacement scope.
    void fetch(endpoint, { method: 'DELETE', headers: headers(view), credentials: 'same-origin',
      cache: 'no-store', keepalive: true }).catch(() => {})
  }

  const clearImage = () => {
    window.clearTimeout(state.current.imageTimer)
    if (state.current.url) URL.revokeObjectURL(state.current.url)
    state.current.url = ''
    setImage(null)
    setAt(null)
  }
  const stop = (notify = true) => {
    const current = state.current
    current.generation += 1
    window.clearTimeout(current.timer)
    current.abort?.abort()
    current.abort = null
    const view = current.view
    current.view = ''
    clearImage()
    setActive(false)
    setBusy(false)
    if (notify && view) {
      // Best effort close, never a renewal. The server also has a 60-second timer.
      closeView(view)
    }
  }

  useEffect(() => {
    const hide = () => { if (document.hidden) stop() }
    const leave = () => stop()
    document.addEventListener('visibilitychange', hide)
    window.addEventListener('pagehide', leave)
    return () => {
      document.removeEventListener('visibilitychange', hide)
      window.removeEventListener('pagehide', leave)
      stop()
    }
    // Closing uses the OLD scope/token. Rebinding never resumes an old grant.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, taskId, authToken])

  const poll = async (generation: number) => {
    if (generation !== state.current.generation || !state.current.view) return
    if (document.hidden || Date.now() >= state.current.expires) {
      stop()
      setNote(zh ? '查看已结束；需要时可重新开启。' : 'Viewing ended. Open it again when needed.')
      return
    }
    const abort = new AbortController()
    const started = performance.now()
    state.current.abort = abort
    const timeout = window.setTimeout(() => abort.abort(), 5000)
    try {
      const response = await fetch(endpoint, { headers: headers(state.current.view), credentials: 'same-origin',
        cache: 'no-store', signal: abort.signal })
      if (generation !== state.current.generation) return
      if (response.status === 204) {
        clearImage()
        setNote(zh ? '等待下一次任务观察；当前没有新画面。' : 'Waiting for the next task observation.')
      } else {
        if (!response.ok || response.headers.get('Content-Type')?.split(';')[0] !== 'image/png') throw new Error('refused')
        const blob = await response.blob()
        if (generation !== state.current.generation) return
        if (!blob.size || blob.size > 2 * 1024 * 1024) throw new Error('invalid')
        const seconds = Number(response.headers.get('X-Ampuly-Frame-At'))
        if (!Number.isFinite(seconds) || seconds <= 0) throw new Error('invalid')
        const remaining = Number(response.headers.get('X-Ampuly-Frame-Remaining-Ms')) - (performance.now() - started)
        if (!Number.isFinite(remaining) || remaining <= 0 || remaining > 2000) {
          clearImage()
          state.current.timer = window.setTimeout(() => { void poll(generation) }, 1000)
          return
        }
        clearImage()
        const url = URL.createObjectURL(blob)
        state.current.url = url
        setImage(url)
        setAt(new Date(seconds * 1000).toLocaleTimeString())
        state.current.imageTimer = window.setTimeout(clearImage, remaining)
        setNote(zh ? '只在任务观察时更新，不是实时视频。' : 'Updated on task observations; this is not live video.')
      }
      if (generation === state.current.generation) {
        state.current.timer = window.setTimeout(() => { void poll(generation) }, 1000)
      }
    } catch {
      if (generation === state.current.generation) {
        stop()
        setNote(zh ? '查看已停止：授权、窗口或连接发生变化。不会自动恢复。' : 'Viewing stopped: authorization, window or connection changed. It will not resume automatically.')
      }
    } finally {
      window.clearTimeout(timeout)
      if (generation === state.current.generation) state.current.abort = null
    }
  }

  const open = async () => {
    stop()
    if (!authToken || document.hidden) return
    const generation = state.current.generation
    setBusy(true)
    setNote('')
    const abort = new AbortController()
    state.current.abort = abort
    const timeout = window.setTimeout(() => abort.abort(), 5000)
    try {
      const response = await fetch(endpoint, { method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { ...headers(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ consent: 'task-window-readonly-v1' }), signal: abort.signal })
      const value = await response.json() as Record<string, unknown>
      if (!response.ok || value.read_only !== true || typeof value.view_id !== 'string'
          || !/^[0-9a-f]{32}$/.test(value.view_id)) throw new Error('refused')
      if (generation !== state.current.generation) {
        // Abort may race a completed POST. Close the now-known stale server view;
        // never activate it or let it occupy a slot until the bounded server TTL.
        closeView(value.view_id)
        return
      }
      state.current.view = value.view_id
      state.current.expires = Date.now() + 60_000
      setActive(true)
      setBusy(false)
      state.current.timer = window.setTimeout(() => { void poll(generation) }, 0)
    } catch {
      if (generation === state.current.generation) {
        stop()
        setNote(zh ? '查看未开启。请确认任务正在观察指定窗口，且已登录负责人账号。' : 'Viewing did not open. Check the active task window and owner login.')
      }
    } finally {
      window.clearTimeout(timeout)
      if (generation === state.current.generation) state.current.abort = null
    }
  }

  return <section className={compact ? 'execution-desktop-view' : undefined} data-testid="desktop-readonly-view" data-fe-eb-22="owner-opt-in" data-view-active={active}>
    {!compact && <p className="shell-card-text">{zh
      ? '点击查看，将本任务指定窗口后续的截图显示在当前控制台。60秒自动停止；不授予鼠标、键盘或模型共享权限。'
      : 'View future screenshots of this task’s window in this console for 60 seconds. This grants no mouse, keyboard or model-sharing permission.'}</p>}
    <button type="button" className="shell-btn" disabled={!authToken || busy} onClick={() => { if (active) stop(); else void open() }}
      title={zh ? '仅显示本任务窗口的观察画面，60秒后停止；不授予操作或模型共享权限。' : 'Display task-window observations for 60 seconds; no input or model-sharing permission.'}>
      {active ? (zh ? '停止查看' : 'Stop viewing') : busy ? (zh ? '正在开启…' : 'Opening…') : (zh ? '查看当前任务窗口' : 'View task window')}
    </button>
    {compact && !image && <div className="execution-surface-empty"><strong>{active ? (zh ? '等待窗口画面' : 'Waiting for the window') : (zh ? '软件窗口尚未显示' : 'Window viewing is off')}</strong>
      <p className="shell-card-text">{zh ? '点击“查看当前任务窗口”显示画面。' : 'Choose “View task window” to display it.'}</p></div>}
    {image && <figure className={compact ? 'execution-desktop-picture' : undefined} style={compact ? undefined : { margin: '12px 0' }}>
      <img src={image} alt={zh ? '当前任务窗口的最近一次观察' : 'Latest observation of the task window'}
        draggable={false} style={compact ? undefined : { display: 'block', maxWidth: '100%', maxHeight: '55vh', objectFit: 'contain' }} />
      <figcaption className="shell-card-text">{zh ? '观察时间：' : 'Observed at: '}{at}</figcaption>
    </figure>}
    {note && <p className="shell-card-text" role="status">{note}</p>}
  </section>
}
