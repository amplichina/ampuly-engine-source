// 数据飞轮 · DATA FLYWHEEL — a paper-engineering blueprint: agents are ink points whose lines
// converge on workflow nodes around a dispatch flywheel; a rotating execution-record ring
// band, gear rings, a scan light and a QC overlay. '粒子荧光' switches to a dark
// fluorescent particle variant. Canvas 2D, original Ampuly code. Theme idea credited to
// cyberdaddyleo/agent-display (PolyForm-Noncommercial) — ideas only, no code used.
import { useMemo, useRef, useState } from 'react'

import { freshPulses, hash01, hexA, taskColor } from '../fx'
import type { RelationNode } from '../relations'
import { useFps } from '../useFps'
import { SimNote, StatStrip, ThemeTitle, TraceRecords } from './ThemeChrome'
import type { ThemeViewProps } from './types'
import { useCanvasStage } from './useCanvasStage'

export const OPS_FLUOR_KEY = 'ampuly.collab.opsFluor'
const MAX_PARTICLES = 420

interface Workflow { id: string; title: string; members: RelationNode[]; angle: number; x: number; y: number; idle: boolean }

function readFluor(): boolean {
  try { return typeof localStorage !== 'undefined' && localStorage.getItem(OPS_FLUOR_KEY) === '1' } catch { return false }
}

function gear(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, teeth: number, angle: number) {
  ctx.beginPath()
  for (let i = 0; i < teeth * 2; i += 1) {
    const a = angle + (i / (teeth * 2)) * Math.PI * 2
    const rr = i % 2 === 0 ? r : r * 0.86
    const a2 = angle + ((i + 1) / (teeth * 2)) * Math.PI * 2
    ctx.lineTo(x + Math.cos(a) * rr, y + Math.sin(a) * rr)
    ctx.lineTo(x + Math.cos(a2) * rr, y + Math.sin(a2) * rr)
  }
  ctx.closePath()
  ctx.stroke()
  ctx.beginPath(); ctx.arc(x, y, r * 0.35, 0, Math.PI * 2); ctx.stroke()
}

export function OpsTheme({ data, model, selectedId, reducedMotion, pulses, onSelect, onExpandFold }: ThemeViewProps) {
  const animate = !reducedMotion
  const fps = useFps(animate)
  const [fluor, setFluor] = useState(readFluor)
  const toggleFluor = () => setFluor(value => {
    try { localStorage.setItem(OPS_FLUOR_KEY, value ? '0' : '1') } catch { /* private mode */ }
    return !value
  })
  const seen = useRef(new Set<string>())
  const flashes = useRef(new Map<string, number>())
  for (const pulse of freshPulses(pulses, seen.current)) flashes.current.set(pulse.actorId, 1)
  const points = useMemo(() => model.members.map(node => ({
    node, u: hash01(`${node.id}:u`), v: hash01(`${node.id}:v`),
  })), [model.members])

  const canvasRef = useCanvasStage(({ ctx, w, h, t, dt, hits }) => {
    const ink = fluor ? '#7dffc8' : '#16171a'
    const inkA = (a: number) => (fluor ? hexA('#7dffc8', a) : `rgba(22,23,26,${a})`)
    // --- paper / dark plate
    ctx.globalCompositeOperation = 'source-over'
    if (fluor) {
      ctx.fillStyle = reducedMotion ? '#04100c' : 'rgba(4,16,12,0.32)'
      ctx.fillRect(0, 0, w, h)
    } else {
      ctx.fillStyle = '#ecebe4'
      ctx.fillRect(0, 0, w, h)
      ctx.lineWidth = 1
      ctx.strokeStyle = 'rgba(30,40,60,0.045)'
      ctx.beginPath()
      for (let x = 0; x < w; x += 14) { ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, h) }
      for (let y = 0; y < h; y += 14) { ctx.moveTo(0, y + 0.5); ctx.lineTo(w, y + 0.5) }
      ctx.stroke()
      ctx.strokeStyle = 'rgba(30,40,60,0.08)'
      ctx.beginPath()
      for (let x = 0; x < w; x += 70) { ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, h) }
      for (let y = 0; y < h; y += 70) { ctx.moveTo(0, y + 0.5); ctx.lineTo(w, y + 0.5) }
      ctx.stroke()
    }

    // --- flywheel geometry
    const fx = w * 0.30
    const fy = h * 0.5
    const R = Math.min(h * 0.44, w * 0.26)
    const spin = t * 0.06
    // arc + ticks (right half of the wheel = where workflows dock)
    ctx.strokeStyle = inkA(0.75)
    ctx.lineWidth = 1.2
    ctx.beginPath(); ctx.arc(fx, fy, R, -Math.PI * 0.55, Math.PI * 0.55); ctx.stroke()
    ctx.lineWidth = 0.8
    ctx.beginPath()
    for (let d = -99; d <= 99; d += 1.5) {
      const a = (d * Math.PI) / 180 + (spin % ((1.5 * Math.PI) / 180))
      const len = Math.round(d) % 15 === 0 ? 12 : 5
      ctx.moveTo(fx + Math.cos(a) * R, fy + Math.sin(a) * R)
      ctx.lineTo(fx + Math.cos(a) * (R + len), fy + Math.sin(a) * (R + len))
    }
    ctx.stroke()
    // gear rings
    ctx.strokeStyle = inkA(0.35)
    ctx.setLineDash([2, 4])
    ctx.beginPath(); ctx.arc(fx, fy, R * 0.62, 0, Math.PI * 2); ctx.stroke()
    ctx.setLineDash([])
    ctx.strokeStyle = inkA(0.5)
    gear(ctx, fx, fy, R * 0.16, 18, spin * 2)
    gear(ctx, fx - R * 0.27, fy + R * 0.12, R * 0.09, 11, -spin * 3.3)

    // --- workflow nodes docked on the arc: one per active task + an idle pool
    const workflowsBase: Omit<Workflow, 'angle' | 'x' | 'y'>[] = model.tasks.slice(0, 7).map(task => ({
      id: task.id, title: task.title, idle: false,
      members: model.members.filter(node => node.active && node.taskId === task.id),
    }))
    const assigned = new Set(workflowsBase.flatMap(flow => flow.members.map(node => node.id)))
    const rest = model.members.filter(node => !assigned.has(node.id))
    workflowsBase.push({ id: '__idle', title: '待命池 · 未在办', idle: true, members: rest })
    const flows: Workflow[] = workflowsBase.map((flow, index) => {
      const n = workflowsBase.length
      const angle = n === 1 ? 0 : -Math.PI * 0.42 + (index / (n - 1)) * Math.PI * 0.84
      return { ...flow, angle, x: fx + Math.cos(angle) * R, y: fy + Math.sin(angle) * R }
    })
    const flowOf = new Map<string, Workflow>()
    for (const flow of flows) for (const node of flow.members) flowOf.set(node.id, flow)

    // agent ink points scattered in the left field
    const agentPos = new Map<string, { x: number; y: number }>()
    for (const point of points) {
      const x = w * 0.03 + point.u * (fx - w * 0.05 + R * 0.2)
      const y = h * 0.2 + point.v * h * 0.66
      agentPos.set(point.node.id, { x, y })
    }

    // converging lines
    ctx.globalCompositeOperation = fluor ? 'lighter' : 'source-over'
    let particleBudget = MAX_PARTICLES
    for (const point of points) {
      const p = agentPos.get(point.node.id)!
      const flow = flowOf.get(point.node.id)
      if (!flow) continue
      const color = !flow.idle ? (fluor ? taskColor(flow.id) : ink) : ink
      const lines = flow.idle ? 1 : 2 + Math.min(4, point.node.activity * 2)
      for (let k = 0; k < lines; k += 1) {
        const jx = (hash01(`${point.node.id}j${k}`) - 0.5) * 26
        const jy = (hash01(`${point.node.id}k${k}`) - 0.5) * 26
        ctx.strokeStyle = flow.idle ? inkA(fluor ? 0.06 : 0.09) : hexA(color, fluor ? 0.22 : 0.45)
        ctx.lineWidth = flow.idle ? 0.5 : 0.7
        ctx.beginPath(); ctx.moveTo(p.x + jx, p.y + jy); ctx.lineTo(flow.x, flow.y); ctx.stroke()
        if (fluor && !flow.idle && particleBudget > 0) {
          particleBudget -= 3
          for (let q = 0; q < 3; q += 1) {
            const s = (t * (0.18 + 0.1 * hash01(`${point.node.id}${k}${q}`)) + q / 3 + hash01(point.node.id)) % 1
            ctx.fillStyle = hexA(taskColor(flow.id), 0.95)
            ctx.fillRect(p.x + jx + (flow.x - p.x - jx) * s - 1, p.y + jy + (flow.y - p.y - jy) * s - 1, 2, 2)
          }
        }
      }
    }
    // agent points + labels
    ctx.globalCompositeOperation = 'source-over'
    for (const point of points) {
      const p = agentPos.get(point.node.id)!
      const flash = flashes.current.get(point.node.id) ?? 0
      if (flash > 0) flashes.current.set(point.node.id, Math.max(0, flash - dt * 0.7))
      const active = point.node.active
      ctx.strokeStyle = inkA(active ? 0.9 : 0.45)
      ctx.fillStyle = active ? (fluor ? taskColor(point.node.taskId ?? point.node.id) : ink) : (fluor ? '#04100c' : '#ecebe4')
      ctx.lineWidth = 1
      if (point.node.kind === 'human') {
        ctx.fillRect(p.x - 3.5, p.y - 3.5, 7, 7); ctx.strokeRect(p.x - 3.5, p.y - 3.5, 7, 7)
      } else {
        ctx.beginPath(); ctx.arc(p.x, p.y, active ? 3.6 : 2.6, 0, Math.PI * 2); ctx.fill(); ctx.stroke()
      }
      if (flash > 0) {
        ctx.strokeStyle = inkA(flash)
        ctx.beginPath(); ctx.arc(p.x, p.y, 4 + (1 - flash) * 22, 0, Math.PI * 2); ctx.stroke()
      }
      if (point.node.id === selectedId) {
        ctx.strokeStyle = fluor ? '#ffffff' : '#c2410c'
        ctx.lineWidth = 1.4
        ctx.strokeRect(p.x - 9, p.y - 9, 18, 18)
      }
      if (active || points.length <= 24 || point.node.id === selectedId) {
        ctx.fillStyle = inkA(active ? 0.95 : 0.55)
        ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace'
        ctx.fillText(point.node.label.slice(0, 8), p.x + 6, p.y - 5)
      }
      hits.push({ id: point.node.id, x: p.x, y: p.y, r: 10 })
    }

    // workflow node labels + code snippets from real trace titles
    flows.forEach((flow, index) => {
      ctx.save()
      ctx.translate(flow.x, flow.y)
      ctx.strokeStyle = inkA(0.8)
      ctx.lineWidth = 1
      ctx.beginPath(); ctx.arc(0, 0, 7, 0, Math.PI * 2); ctx.stroke()
      ctx.setLineDash([2, 3])
      ctx.beginPath(); ctx.arc(0, 0, 16 + Math.sin(t + index) * 2, spin * 4, spin * 4 + Math.PI * 1.5); ctx.stroke()
      ctx.setLineDash([])
      const label = `W${String(index + 1).padStart(2, '0')} · ${flow.title.length > 12 ? `${flow.title.slice(0, 12)}…` : flow.title}`
      ctx.font = '600 11px ui-monospace, SFMono-Regular, Menlo, monospace'
      const lw = ctx.measureText(label).width + 12
      ctx.fillStyle = flow.idle ? (fluor ? 'rgba(125,255,200,0.15)' : 'rgba(22,23,26,0.12)') : (fluor ? hexA(taskColor(flow.id), 0.85) : '#16171a')
      ctx.fillRect(-lw - 12, -9, lw, 18)
      ctx.fillStyle = flow.idle ? ink : (fluor ? '#04100c' : '#f2f1ea')
      ctx.fillText(label, -lw - 6, 4)
      ctx.font = '9px ui-monospace, SFMono-Regular, Menlo, monospace'
      ctx.fillStyle = inkA(0.6)
      ctx.fillText(`${flow.members.length} ${flow.idle ? 'idle' : 'agents'} · ${flow.idle ? '—' : 'running'}`, -lw - 10, 20)
      // tangent code block (real trace titles of this workflow's members)
      const memberIds = new Set(flow.members.map(node => node.id))
      const lines = model.events.filter(event => memberIds.has(event.actorId)).slice(-4).map(event => `› ${(event.title || event.type || 'event').slice(0, 26)}`)
      if (!flow.idle) {
        ctx.rotate(flow.angle + Math.PI / 2 - Math.PI / 2)
        ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace'
        ctx.fillStyle = inkA(0.85)
        ctx.fillText(`// ${flow.id}`, 26, -6)
        lines.forEach((line, row) => ctx.fillText(line, 26, 8 + row * 12))
      }
      ctx.restore()
    })

    // --- rotating execution-record ring band (right)
    const cx = w * 1.06
    const cy = h * 0.5
    const r2 = h * 0.62
    const rot = reducedMotion ? 0 : t * 0.035
    ctx.globalCompositeOperation = fluor ? 'lighter' : 'source-over'
    ctx.lineWidth = 1
    for (let i = 0; i < 260; i += 1) {
      const a = Math.PI * 0.62 + (i / 260) * Math.PI * 0.76 + rot
      const len = 18 + hash01(`bar${i}`) * (i % 17 === 0 ? 220 : 120)
      const heat = model.activeMembers / Math.max(1, model.members.length + model.hiddenMembers)
      ctx.strokeStyle = fluor ? hexA(i % 9 === 0 ? '#ff7ad9' : '#7dffc8', 0.25 + 0.4 * heat) : inkA(0.25 + 0.5 * hash01(`ba${i}`))
      ctx.beginPath()
      ctx.moveTo(cx + Math.cos(a) * r2, cy + Math.sin(a) * r2)
      ctx.lineTo(cx + Math.cos(a) * (r2 - len), cy + Math.sin(a) * (r2 - len))
      ctx.stroke()
    }
    ctx.strokeStyle = inkA(0.8)
    ctx.beginPath(); ctx.arc(cx, cy, r2 + 6, Math.PI * 0.6, Math.PI * 1.4); ctx.stroke()
    ctx.beginPath()
    for (let d = 0; d < 80; d += 1) {
      const a = Math.PI * 0.6 + (d / 80) * Math.PI * 0.8 + rot
      ctx.moveTo(cx + Math.cos(a) * (r2 + 6), cy + Math.sin(a) * (r2 + 6))
      ctx.lineTo(cx + Math.cos(a) * (r2 + (d % 5 === 0 ? 16 : 10)), cy + Math.sin(a) * (r2 + (d % 5 === 0 ? 16 : 10)))
    }
    ctx.stroke()
    // record text riding the ring (real trace titles)
    ctx.globalCompositeOperation = 'source-over'
    ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace'
    const records = model.events.slice(-14)
    records.forEach((event, index) => {
      const a = Math.PI * 0.7 + (index / Math.max(1, records.length)) * Math.PI * 0.6 + rot
      const x = cx + Math.cos(a) * (r2 + 26)
      const y = cy + Math.sin(a) * (r2 + 26)
      if (x < 0 || x > w || y < 0 || y > h) return
      ctx.save()
      ctx.translate(x, y)
      ctx.rotate(a + Math.PI)
      ctx.fillStyle = inkA(0.85)
      ctx.fillText(`${(event.title || event.type || 'event').slice(0, 22)}`, 0, 0)
      ctx.restore()
    })

    // --- scan light + scanline
    if (!reducedMotion) {
      const sx = ((t * 0.08) % 1.2) * w - w * 0.1
      const g = ctx.createLinearGradient(sx - 80, 0, sx + 80, 0)
      const c = fluor ? '125,255,200' : '255,140,40'
      g.addColorStop(0, `rgba(${c},0)`); g.addColorStop(0.5, `rgba(${c},${fluor ? 0.10 : 0.08})`); g.addColorStop(1, `rgba(${c},0)`)
      ctx.fillStyle = g
      ctx.fillRect(sx - 80, 0, 160, h)
      ctx.strokeStyle = `rgba(${c},0.5)`
      ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, h); ctx.stroke()
    }

    // --- QC overlay (real numbers)
    const qx = w * 0.70
    const qy = h * 0.70
    ctx.fillStyle = inkA(0.85)
    ctx.fillRect(qx, qy, 2, 46)
    ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace'
    ctx.fillText('STABILITY CHECK · 质检', qx + 10, qy + 10)
    ctx.font = '600 12px ui-monospace, SFMono-Regular, Menlo, monospace'
    ctx.fillStyle = model.blocked ? (fluor ? '#ff7a8a' : '#b42318') : inkA(0.95)
    ctx.fillText(`阻塞 ${model.blocked} · 待你 ${model.waiting} · 在办 ${model.activeMembers}/${model.members.length + model.hiddenMembers}`, qx + 10, qy + 28)
    ctx.fillStyle = inkA(0.6)
    ctx.font = '9px ui-monospace, SFMono-Regular, Menlo, monospace'
    ctx.fillText(model.blocked ? '— 存在阻塞，请在右侧值班台处理' : '— 当前无阻塞', qx + 10, qy + 42)

    // folds
    model.folds.forEach((fold, index) => {
      const x = w * 0.04 + index * 46
      const y = h * 0.93
      ctx.strokeStyle = inkA(0.6)
      ctx.setLineDash([3, 3]); ctx.strokeRect(x, y - 10, 40, 18); ctx.setLineDash([])
      ctx.fillStyle = inkA(0.8)
      ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace'
      ctx.fillText(`+${fold.count ?? 0}`, x + 8, y + 3)
      hits.push({ id: fold.id, x: x + 20, y, r: 16 })
    })
  }, {
    animate,
    deps: [model, selectedId, fluor],
    onHit: id => (id.startsWith('fold:') ? onExpandFold(id.slice('fold:'.length)) : onSelect(id)),
  })

  return (
    <div className={`collab-relations collab-theme is-ops${fluor ? ' is-fluor' : ''}`} data-testid="collab-theme-ops" data-theme="ops" data-fluor={fluor ? '1' : '0'}>
      <canvas ref={canvasRef} className="collab-theme-canvas" aria-label="数据飞轮：协作关系主题视图" role="img" />
      <div className="collab-theme-hud">
        <ThemeTitle en="DATA FLYWHEEL" zh="数据飞轮" tagline={`调度飞轮 · ${model.tasks.length} 条工作流汇聚 · 执行记录环带`} />
        <StatStrip model={model} fps={fps} extra={[['协作流 FLOWS', data.activeLinks]]} />
      </div>
      <button type="button" className={`collab-theme-toggle${fluor ? ' is-active' : ''}`} aria-pressed={fluor} onClick={toggleFluor}>
        粒子荧光
      </button>
      <TraceRecords model={model} title="TASK FEED · 执行记录" />
      <SimNote />
    </div>
  )
}

export default OpsTheme
