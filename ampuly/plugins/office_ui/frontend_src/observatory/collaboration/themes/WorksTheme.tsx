// 天工开物 · HEAVEN WORKS — a dispatch satellite beams work to a row of loom-machines
// (the agents); working looms spin glowing silk threads down onto a project fabric,
// one woven section per active task. Canvas 2D, original Ampuly code. Theme idea credited
// to cyberdaddyleo/agent-display (PolyForm-Noncommercial) — ideas only, no code used.
import { useMemo, useRef } from 'react'

import { FX_COLORS, freshPulses, hash01, hexA, makeStars, taskColor } from '../fx'
import type { RelationNode } from '../relations'
import { useFps } from '../useFps'
import { SimNote, StatStrip, ThemeTitle, TraceRecords } from './ThemeChrome'
import type { ThemeViewProps } from './types'
import { useCanvasStage } from './useCanvasStage'

const MAX_THREADS = 150
const PER_ROW = 16

interface Loom { node: RelationNode; x: number; y: number; r: number }

function drawSatellite(ctx: CanvasRenderingContext2D, x: number, y: number, s: number, t: number) {
  ctx.save()
  ctx.translate(x, y + Math.sin(t * 0.8) * 3 * s)
  // solar panels
  for (const side of [-1, 1]) {
    const px = side < 0 ? -118 * s : 34 * s
    const grad = ctx.createLinearGradient(px, -16 * s, px + 84 * s, 16 * s)
    grad.addColorStop(0, '#1b2f5c'); grad.addColorStop(0.5, '#3a5fa8'); grad.addColorStop(1, '#16264a')
    ctx.fillStyle = grad
    ctx.fillRect(px, -16 * s, 84 * s, 32 * s)
    ctx.strokeStyle = 'rgba(190,215,255,0.55)'
    ctx.lineWidth = 0.7
    ctx.strokeRect(px, -16 * s, 84 * s, 32 * s)
    ctx.beginPath()
    for (let i = 1; i < 7; i += 1) { ctx.moveTo(px + i * 12 * s, -16 * s); ctx.lineTo(px + i * 12 * s, 16 * s) }
    ctx.moveTo(px, 0); ctx.lineTo(px + 84 * s, 0)
    ctx.stroke()
    ctx.strokeStyle = 'rgba(220,220,220,0.8)'
    ctx.beginPath(); ctx.moveTo(side * 22 * s, 0); ctx.lineTo(side < 0 ? px + 84 * s : px, 0); ctx.stroke()
  }
  // body (gold foil)
  const body = ctx.createLinearGradient(-22 * s, -24 * s, 22 * s, 24 * s)
  body.addColorStop(0, '#f6e7b0'); body.addColorStop(0.45, '#c9a14a'); body.addColorStop(1, '#7a5a1c')
  ctx.fillStyle = body
  ctx.fillRect(-22 * s, -24 * s, 44 * s, 44 * s)
  ctx.strokeStyle = 'rgba(255,240,200,0.7)'
  ctx.strokeRect(-22 * s, -24 * s, 44 * s, 44 * s)
  ctx.fillStyle = 'rgba(40,30,10,0.45)'
  for (let i = 0; i < 4; i += 1) ctx.fillRect(-16 * s + i * 9 * s, -18 * s, 5 * s, 30 * s)
  // dish
  ctx.fillStyle = '#e8eef6'
  ctx.beginPath(); ctx.ellipse(0, 30 * s, 26 * s, 9 * s, 0, 0, Math.PI); ctx.fill()
  ctx.strokeStyle = 'rgba(120,140,170,0.9)'
  ctx.beginPath(); ctx.ellipse(0, 30 * s, 26 * s, 4 * s, 0, 0, Math.PI * 2); ctx.stroke()
  // beacon
  const glow = ctx.createRadialGradient(0, 40 * s, 0, 0, 40 * s, 40 * s)
  glow.addColorStop(0, `rgba(160,230,255,${0.55 + 0.25 * Math.sin(t * 3)})`); glow.addColorStop(1, 'rgba(160,230,255,0)')
  ctx.globalCompositeOperation = 'lighter'
  ctx.fillStyle = glow
  ctx.beginPath(); ctx.arc(0, 40 * s, 40 * s, 0, Math.PI * 2); ctx.fill()
  ctx.restore()
}

function drawLoom(ctx: CanvasRenderingContext2D, loom: Loom, color: string, t: number, selected: boolean, flash: number) {
  const { x, y, r, node } = loom
  ctx.save()
  ctx.translate(x, y)
  if (node.active || flash > 0) {
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, r * (3.2 + flash * 3))
    g.addColorStop(0, hexA(color, 0.55 + flash * 0.4)); g.addColorStop(1, hexA(color, 0))
    ctx.globalCompositeOperation = 'lighter'
    ctx.fillStyle = g
    ctx.beginPath(); ctx.arc(0, 0, r * (3.2 + flash * 3), 0, Math.PI * 2); ctx.fill()
    ctx.globalCompositeOperation = 'source-over'
  }
  if (node.kind === 'human') {
    ctx.rotate(Math.PI / 4)
    ctx.fillStyle = node.active ? '#3a2a10' : '#1a1a20'
    ctx.strokeStyle = node.active ? FX_COLORS.human : 'rgba(255,198,90,0.35)'
    ctx.lineWidth = 1.2
    ctx.fillRect(-r * 0.75, -r * 0.75, r * 1.5, r * 1.5)
    ctx.strokeRect(-r * 0.75, -r * 0.75, r * 1.5, r * 1.5)
    ctx.rotate(-Math.PI / 4)
  } else {
    // little machine: mast, wings and a spinning spool ring
    ctx.strokeStyle = node.active ? 'rgba(230,240,255,0.85)' : 'rgba(150,170,200,0.35)'
    ctx.lineWidth = 1
    ctx.beginPath(); ctx.moveTo(0, -r * 1.9); ctx.lineTo(0, -r); ctx.stroke()
    ctx.fillStyle = node.active ? '#dfe7f2' : '#2a3140'
    ctx.fillRect(-r * 1.6, -r * 0.25, r * 3.2, r * 0.5)
    const body = ctx.createLinearGradient(-r, -r, r, r)
    body.addColorStop(0, node.active ? '#ffffff' : '#465063'); body.addColorStop(1, node.active ? '#8c97a8' : '#1c2230')
    ctx.fillStyle = body
    ctx.beginPath(); ctx.arc(0, 0, r, 0, Math.PI * 2); ctx.fill()
    ctx.strokeStyle = node.active ? color : 'rgba(120,140,170,0.35)'
    ctx.lineWidth = 1.4
    ctx.setLineDash([r * 0.6, r * 0.4])
    ctx.lineDashOffset = node.active ? -t * 30 : 0
    ctx.beginPath(); ctx.arc(0, 0, r * 1.45, 0, Math.PI * 2); ctx.stroke()
    ctx.setLineDash([])
  }
  if (selected) {
    ctx.strokeStyle = '#ffffff'
    ctx.lineWidth = 1.2
    ctx.beginPath(); ctx.arc(0, 0, r * 2.3, 0, Math.PI * 2); ctx.stroke()
  }
  if (flash > 0) {
    ctx.strokeStyle = hexA('#ffffff', flash)
    ctx.beginPath(); ctx.arc(0, 0, r * (2 + (1 - flash) * 5), 0, Math.PI * 2); ctx.stroke()
  }
  ctx.restore()
}

export function WorksTheme({ data, model, selectedId, reducedMotion, pulses, onSelect, onExpandFold }: ThemeViewProps) {
  const animate = !reducedMotion
  const fps = useFps(animate)
  const stars = useMemo(() => makeStars(260, 11), [])
  const seen = useRef(new Set<string>())
  const flashes = useRef(new Map<string, number>())
  const fresh = freshPulses(pulses, seen.current)
  for (const pulse of fresh) flashes.current.set(pulse.actorId, 1)
  const weave = useRef(new Map<string, number>())

  const canvasRef = useCanvasStage(({ ctx, w, h, t, dt, hits }) => {
    // --- background: deep night + falling data rain
    const bg = ctx.createLinearGradient(0, 0, 0, h)
    bg.addColorStop(0, '#05060c'); bg.addColorStop(0.6, '#070a14'); bg.addColorStop(1, '#0b0f1a')
    ctx.globalCompositeOperation = 'source-over'
    ctx.fillStyle = bg
    ctx.fillRect(0, 0, w, h)
    for (const star of stars) {
      const a = star.base * (0.6 + 0.4 * Math.sin(t * star.speed + star.phase))
      ctx.fillStyle = `rgba(200,215,255,${a * 0.7})`
      ctx.fillRect(star.x * w, star.y * h * 0.75, star.r, star.r)
    }
    ctx.strokeStyle = 'rgba(120,170,255,0.05)'
    ctx.lineWidth = 1
    ctx.beginPath()
    for (let i = 0; i < 40; i += 1) {
      const x = hash01(`rain${i}`) * w
      const y = ((hash01(`rainy${i}`) * h + t * 40 * (0.5 + hash01(`rs${i}`))) % (h * 1.2)) - h * 0.2
      ctx.moveTo(x, y); ctx.lineTo(x, y + 40 + 60 * hash01(`rl${i}`))
    }
    ctx.stroke()

    // --- layout
    const sat = { x: w * 0.5, y: h * 0.13 }
    const scale = Math.max(0.55, Math.min(1, w / 1500))
    const members = model.members
    const rows = Math.max(1, Math.ceil(members.length / PER_ROW))
    const looms: Loom[] = members.map((node, index) => {
      const row = Math.floor(index / PER_ROW)
      const inRow = Math.min(PER_ROW, members.length - row * PER_ROW)
      const col = index - row * PER_ROW
      const span = w * (0.62 - row * 0.06)
      const x = w * 0.5 - span / 2 + (inRow === 1 ? span / 2 : (span * col) / (inRow - 1))
      const arch = Math.sin((col / Math.max(1, inRow - 1)) * Math.PI) * h * 0.04
      return { node, x, y: h * (0.36 + row * 0.075) - arch, r: Math.max(4, (node.active ? 9 : 6) * scale * (rows > 2 ? 0.8 : 1)) }
    })
    const fabric = { top: h * 0.72, bottom: h * 0.98, topHalf: w * 0.27, bottomHalf: w * 0.45 }
    const sections = model.tasks.length ? model.tasks : []
    const sectionCount = Math.max(1, sections.length)
    const fabricX = (u: number, v: number) => {
      const half = fabric.topHalf + (fabric.bottomHalf - fabric.topHalf) * v
      return w * 0.5 - half + 2 * half * u
    }
    const fabricY = (v: number) => fabric.top + (fabric.bottom - fabric.top) * v

    // --- dispatch beams satellite -> looms
    ctx.globalCompositeOperation = 'lighter'
    for (const loom of looms) {
      const color = loom.node.taskId ? taskColor(loom.node.taskId) : FX_COLORS.ai
      ctx.strokeStyle = loom.node.active ? hexA(color, 0.35) : 'rgba(120,150,200,0.07)'
      ctx.lineWidth = loom.node.active ? 1 : 0.6
      ctx.setLineDash(loom.node.active ? [6, 10] : [])
      ctx.lineDashOffset = -t * 40
      ctx.beginPath(); ctx.moveTo(sat.x, sat.y + 40 * scale); ctx.lineTo(loom.x, loom.y - loom.r * 2); ctx.stroke()
      if (loom.node.active) {
        const s = (t * 0.45 + hash01(loom.node.id)) % 1
        const px = sat.x + (loom.x - sat.x) * s
        const py = sat.y + 40 * scale + (loom.y - loom.r * 2 - sat.y - 40 * scale) * s
        ctx.fillStyle = hexA('#ffffff', 0.9)
        ctx.beginPath(); ctx.arc(px, py, 1.8, 0, Math.PI * 2); ctx.fill()
      }
    }
    ctx.setLineDash([])

    // --- fabric plane (perspective grid + contours)
    ctx.globalCompositeOperation = 'source-over'
    ctx.beginPath()
    ctx.moveTo(fabricX(0, 0), fabricY(0)); ctx.lineTo(fabricX(1, 0), fabricY(0))
    ctx.lineTo(fabricX(1, 1), fabricY(1)); ctx.lineTo(fabricX(0, 1), fabricY(1)); ctx.closePath()
    const cloth = ctx.createLinearGradient(0, fabric.top, 0, fabric.bottom)
    cloth.addColorStop(0, 'rgba(30,40,60,0.75)'); cloth.addColorStop(1, 'rgba(12,16,26,0.95)')
    ctx.fillStyle = cloth
    ctx.fill()
    ctx.strokeStyle = 'rgba(200,220,255,0.35)'
    ctx.stroke()
    ctx.strokeStyle = 'rgba(170,200,255,0.08)'
    ctx.lineWidth = 0.7
    ctx.beginPath()
    for (let i = 1; i < 30; i += 1) { ctx.moveTo(fabricX(i / 30, 0), fabricY(0)); ctx.lineTo(fabricX(i / 30, 1), fabricY(1)) }
    for (let j = 1; j < 10; j += 1) { const v = (j / 10) ** 1.3; ctx.moveTo(fabricX(0, v), fabricY(v)); ctx.lineTo(fabricX(1, v), fabricY(v)) }
    ctx.stroke()
    ctx.strokeStyle = 'rgba(220,230,255,0.10)'
    for (let c = 0; c < 7; c += 1) {
      ctx.beginPath()
      for (let k = 0; k <= 40; k += 1) {
        const u = k / 40
        const v = 0.2 + c * 0.11 + 0.04 * Math.sin(u * 9 + c * 1.7)
        const x = fabricX(u, v); const y = fabricY(v)
        if (k === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
      }
      ctx.stroke()
    }

    // --- woven sections (one per active task); weave level is decorative and labelled
    ctx.globalCompositeOperation = 'lighter'
    sections.forEach((task, index) => {
      const u0 = index / sectionCount + 0.01
      const u1 = (index + 1) / sectionCount - 0.01
      const color = taskColor(task.id)
      const current = weave.current.get(task.id) ?? 0.08
      const next = Math.min(1, current + dt * 0.02 * task.memberIds.length)
      weave.current.set(task.id, next >= 1 ? 0.08 : next)
      const depth = reducedMotion ? 0.6 : next
      ctx.beginPath()
      ctx.moveTo(fabricX(u0, 0), fabricY(0)); ctx.lineTo(fabricX(u1, 0), fabricY(0))
      ctx.lineTo(fabricX(u1, depth), fabricY(depth)); ctx.lineTo(fabricX(u0, depth), fabricY(depth)); ctx.closePath()
      const g = ctx.createLinearGradient(0, fabricY(0), 0, fabricY(depth))
      g.addColorStop(0, hexA(color, 0.32)); g.addColorStop(1, hexA(color, 0.05))
      ctx.fillStyle = g
      ctx.fill()
      // weft line at the weaving front
      ctx.strokeStyle = hexA(color, 0.9)
      ctx.lineWidth = 1.4
      ctx.beginPath(); ctx.moveTo(fabricX(u0, depth), fabricY(depth)); ctx.lineTo(fabricX(u1, depth), fabricY(depth)); ctx.stroke()
      // section label
      ctx.globalCompositeOperation = 'source-over'
      const lx = fabricX(u0, 0)
      const lw = fabricX(u1, 0) - lx
      ctx.fillStyle = 'rgba(8,12,20,0.85)'
      ctx.fillRect(lx, fabric.top - 30, lw, 24)
      ctx.strokeStyle = hexA(color, 0.6)
      ctx.lineWidth = 1
      ctx.strokeRect(lx, fabric.top - 30, lw, 24)
      ctx.fillStyle = color
      ctx.font = '600 10px ui-monospace, SFMono-Regular, Menlo, monospace'
      ctx.fillText(`S${String(index + 1).padStart(2, '0')} · ${task.memberIds.length} 台`, lx + 6, fabric.top - 19)
      ctx.fillStyle = '#e8eefc'
      ctx.font = '11px system-ui, sans-serif'
      const title = task.title.length > Math.max(4, Math.floor(lw / 12)) ? `${task.title.slice(0, Math.max(3, Math.floor(lw / 12) - 1))}…` : task.title
      ctx.fillText(title, lx + 6, fabric.top - 9)
      ctx.globalCompositeOperation = 'lighter'
    })
    if (!sections.length) {
      ctx.globalCompositeOperation = 'source-over'
      ctx.fillStyle = 'rgba(200,215,240,0.6)'
      ctx.font = '12px system-ui, sans-serif'
      ctx.textAlign = 'center'
      ctx.fillText('暂无在办任务 · 织机待命', w * 0.5, fabricY(0.4))
      ctx.textAlign = 'start'
      ctx.globalCompositeOperation = 'lighter'
    }

    // --- silk threads loom -> section on the fabric
    let threadBudget = MAX_THREADS
    for (const loom of looms) {
      if (!loom.node.active || threadBudget <= 0) continue
      const sectionIndex = Math.max(0, sections.findIndex(task => task.id === loom.node.taskId))
      const color = loom.node.taskId ? taskColor(loom.node.taskId) : FX_COLORS.ai
      const count = Math.min(threadBudget, 3 + Math.min(4, loom.node.activity * 2))
      threadBudget -= count
      for (let k = 0; k < count; k += 1) {
        const seed = hash01(`${loom.node.id}:${k}`)
        const u = (sectionIndex + 0.15 + 0.7 * seed) / sectionCount
        const v = 0.05 + 0.25 * hash01(`${loom.node.id}v${k}`)
        const ex = fabricX(u, v)
        const ey = fabricY(v)
        const sway = Math.sin(t * (0.6 + seed) + seed * 9) * 26
        const c1x = loom.x + (seed - 0.5) * 80 + sway
        const c1y = loom.y + (ey - loom.y) * 0.25
        const c2x = ex + sway * 0.6
        const c2y = loom.y + (ey - loom.y) * 0.7
        ctx.strokeStyle = hexA(color, 0.10)
        ctx.lineWidth = 4
        ctx.beginPath(); ctx.moveTo(loom.x, loom.y + loom.r); ctx.bezierCurveTo(c1x, c1y, c2x, c2y, ex, ey); ctx.stroke()
        ctx.strokeStyle = hexA(k % 3 === 0 ? '#ffffff' : color, 0.55)
        ctx.lineWidth = 0.9
        ctx.stroke()
        // bead travelling down the thread
        const s = (t * (0.25 + 0.2 * seed) + seed) % 1
        const mt = 1 - s
        const bx = mt ** 3 * loom.x + 3 * mt * mt * s * c1x + 3 * mt * s * s * c2x + s ** 3 * ex
        const by = mt ** 3 * (loom.y + loom.r) + 3 * mt * mt * s * c1y + 3 * mt * s * s * c2y + s ** 3 * ey
        ctx.fillStyle = hexA('#ffffff', 0.95)
        ctx.beginPath(); ctx.arc(bx, by, 1.6, 0, Math.PI * 2); ctx.fill()
        // landing splash
        const splash = ctx.createRadialGradient(ex, ey, 0, ex, ey, 10)
        splash.addColorStop(0, hexA(color, 0.6)); splash.addColorStop(1, hexA(color, 0))
        ctx.fillStyle = splash
        ctx.beginPath(); ctx.arc(ex, ey, 10, 0, Math.PI * 2); ctx.fill()
      }
    }

    // --- looms + labels
    ctx.globalCompositeOperation = 'source-over'
    for (const loom of looms) {
      const color = loom.node.taskId ? taskColor(loom.node.taskId) : loom.node.kind === 'human' ? FX_COLORS.human : FX_COLORS.ai
      const flash = flashes.current.get(loom.node.id) ?? 0
      if (flash > 0) flashes.current.set(loom.node.id, Math.max(0, flash - dt * 0.8))
      drawLoom(ctx, loom, color, t, loom.node.id === selectedId, flash)
      hits.push({ id: loom.node.id, x: loom.x, y: loom.y, r: Math.max(12, loom.r * 2) })
      if (loom.node.active || members.length <= 20 || loom.node.id === selectedId) {
        ctx.fillStyle = loom.node.active ? '#eaf2ff' : 'rgba(180,195,220,0.55)'
        ctx.font = `${loom.node.active ? 600 : 400} 10px system-ui, sans-serif`
        ctx.textAlign = 'center'
        ctx.fillText(loom.node.label.length > 6 ? `${loom.node.label.slice(0, 6)}…` : loom.node.label, loom.x, loom.y - loom.r * 2.4)
        ctx.textAlign = 'start'
      }
    }
    // folded branches as dim "+N" pods at the right end
    model.folds.forEach((fold, index) => {
      const x = w * 0.84 + (index % 3) * 34
      const y = h * 0.36 + Math.floor(index / 3) * 30
      ctx.strokeStyle = 'rgba(160,175,200,0.5)'
      ctx.setLineDash([3, 3])
      ctx.beginPath(); ctx.arc(x, y, 11, 0, Math.PI * 2); ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = 'rgba(200,210,230,0.8)'
      ctx.font = '10px system-ui, sans-serif'
      ctx.textAlign = 'center'
      ctx.fillText(`+${fold.count ?? 0}`, x, y + 3)
      ctx.textAlign = 'start'
      hits.push({ id: fold.id, x, y, r: 14 })
    })

    drawSatellite(ctx, sat.x, sat.y, scale, t)
    ctx.fillStyle = '#ffe8a8'
    ctx.font = '600 11px ui-monospace, SFMono-Regular, Menlo, monospace'
    ctx.textAlign = 'center'
    ctx.fillText(`调度卫星 · ${model.owner?.label ?? 'DISPATCH'}`, sat.x, sat.y + 74 * scale)
    ctx.textAlign = 'start'
  }, {
    animate,
    deps: [model, selectedId],
    onHit: id => (id.startsWith('fold:') ? onExpandFold(id.slice('fold:'.length)) : onSelect(id)),
  })

  return (
    <div className="collab-relations collab-theme is-works" data-testid="collab-theme-works" data-theme="works">
      <canvas ref={canvasRef} className="collab-theme-canvas" aria-label="天工开物：协作关系主题视图" role="img" />
      <div className="collab-theme-hud">
        <ThemeTitle en="HEAVEN WORKS" zh="天工开物" tagline={`调度卫星派工 · ${model.activeMembers} 台织机吐丝 · ${model.tasks.length} 块任务织面`} />
        <StatStrip model={model} fps={fps} extra={[['协作流 FLOWS', data.activeLinks]]} />
      </div>
      <TraceRecords model={model} title="04 / 交付记录 · DELIVERED" />
      <SimNote />
    </div>
  )
}

export default WorksTheme
