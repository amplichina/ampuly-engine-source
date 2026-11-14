// 关系 view: force-directed, draggable collaboration graph with flowing particles on
// active relations.
//
// Rendering, force simulation, drag/zoom and directional particles are provided by
// react-force-graph-2d / force-graph (MIT, Copyright (c) 2018 Vasco Asturiano),
// https://github.com/vasturiano/react-force-graph and
// https://github.com/vasturiano/force-graph — installed as an npm dependency, not
// vendored; see THIRD_PARTY_NOTICES.md. The node/link painting below, the theme
// bridge and the data shaping (relations.ts) are original Ampuly code. Visual idea
// (star-map style agent graph) credited to useroneoneone/neural-vault; none of its
// PolyForm-Noncommercial code is used.
import { useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph2D, { type ForceGraphMethods, type LinkObject, type NodeObject } from 'react-force-graph-2d'

import type { RelationGraphData, RelationLink, RelationNode } from './relations'

type GNode = NodeObject<RelationNode>
type GLink = LinkObject<RelationNode, RelationLink>

interface Palette {
  accent: string
  yellow: string
  green: string
  red: string
  text: string
  muted: string
  line: string
  panel: string
}

const FALLBACK: Palette = {
  accent: '#087bbf', yellow: '#9b6519', green: '#287762', red: '#b3362f',
  text: '#102e49', muted: '#61768a', line: '#d6e3ee', panel: '#ffffff',
}

/** Resolve the shell VI tokens to concrete colors the canvas can use. */
function readPalette(host: HTMLElement | null): Palette {
  if (!host || typeof window === 'undefined') return FALLBACK
  const probe = document.createElement('span')
  probe.style.display = 'none'
  host.appendChild(probe)
  const read = (token: string, fallback: string) => {
    probe.style.color = ''
    probe.style.color = `var(${token}, ${fallback})`
    const value = getComputedStyle(probe).color
    return value && value !== 'rgba(0, 0, 0, 0)' ? value : fallback
  }
  const palette: Palette = {
    accent: read('--collab-accent', FALLBACK.accent),
    yellow: read('--collab-yellow', FALLBACK.yellow),
    green: read('--collab-green', FALLBACK.green),
    red: read('--collab-red', FALLBACK.red),
    text: read('--collab-text', FALLBACK.text),
    muted: read('--collab-muted', FALLBACK.muted),
    line: read('--collab-line', FALLBACK.line),
    panel: read('--collab-panel', FALLBACK.panel),
  }
  probe.remove()
  return palette
}

function alpha(color: string, value: number): string {
  const match = color.match(/rgba?\(([^)]+)\)/)
  if (!match) return color
  const [r, g, b] = match[1].split(/[ ,/]+/).filter(Boolean)
  return `rgba(${r}, ${g}, ${b}, ${value})`
}

function endpointId(end: GLink['source']): string {
  if (end && typeof end === 'object') return String((end as { id?: string | number }).id ?? '')
  return String(end ?? '')
}

export interface RelationsGraphProps {
  data: RelationGraphData
  selectedId?: string
  reducedMotion?: boolean
  onSelect?: (id: string) => void
  onExpandFold?: (managerId: string) => void
}

export function RelationsGraph({ data, selectedId, reducedMotion = false, onSelect, onExpandFold }: RelationsGraphProps) {
  const hostRef = useRef<HTMLDivElement>(null)
  const graphRef = useRef<ForceGraphMethods<GNode, GLink> | undefined>(undefined)
  const positions = useRef(new Map<string, { x: number; y: number; fx?: number; fy?: number }>())
  const fitted = useRef(false)
  const userMoved = useRef(false)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [palette, setPalette] = useState<Palette>(FALLBACK)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const update = () => setSize({ width: host.clientWidth, height: host.clientHeight })
    update()
    setPalette(readPalette(host))
    const observer = new ResizeObserver(update)
    observer.observe(host)
    // Theme switches change data-palette on the shell; re-read the tokens.
    const shell = host.closest('.app-shell') ?? document.documentElement
    const themeObserver = new MutationObserver(() => setPalette(readPalette(host)))
    themeObserver.observe(shell, { attributes: true, attributeFilter: ['data-palette', 'data-theme', 'class'] })
    return () => { observer.disconnect(); themeObserver.disconnect() }
  }, [])

  // Keep positions (and pins) across data refreshes so live updates do not reshuffle.
  const graphData = useMemo(() => {
    const nodes: GNode[] = data.nodes.map(node => {
      const previous = positions.current.get(node.id)
      const next: GNode = { ...node, ...(previous ?? {}) }
      if (node.isOwner && !previous) { next.fx = 0; next.fy = 0 }
      return next
    })
    const links: GLink[] = data.links.map(link => ({ ...link }))
    return { nodes, links }
  }, [data])

  useEffect(() => {
    const graph = graphRef.current
    if (!graph) return
    graph.d3Force('charge')?.strength?.(-280)
    const linkForce = graph.d3Force('link')
    linkForce?.distance?.((link: GLink) => (link.kind === 'reports' ? 78 : 110))
    linkForce?.strength?.((link: GLink) => (link.kind === 'reports' ? 0.7 : 0.18))
    graph.d3ReheatSimulation()
  }, [graphData, size.width])

  // Fit the whole constellation once it has spread out (the user can zoom/drag after).
  useEffect(() => {
    if (!size.width || !size.height) return
    fitted.current = false
    const timer = window.setTimeout(() => {
      if (!userMoved.current) graphRef.current?.zoomToFit(reducedMotion ? 0 : 400, 56)
    }, reducedMotion ? 50 : 700)
    return () => window.clearTimeout(timer)
  }, [graphData, size.width, size.height, reducedMotion])

  const remember = () => {
    for (const node of graphData.nodes) {
      if (node.x === undefined || node.y === undefined) continue
      positions.current.set(String(node.id), {
        x: node.x, y: node.y,
        ...(node.fx !== undefined && node.fx !== null ? { fx: node.fx, fy: node.fy ?? node.y } : {}),
      })
    }
  }

  const tone = (node: RelationNode) => (node.kind === 'human' ? palette.yellow : node.kind === 'fold' ? palette.muted : palette.accent)
  const stateColor = (node: RelationNode) => node.state === 'blocked' ? palette.red
    : node.state === 'waiting' ? palette.yellow
      : node.state === 'working' ? palette.green
        : palette.muted

  const paintNode = (raw: GNode, ctx: CanvasRenderingContext2D, scale: number) => {
    const node = raw as GNode & RelationNode
    const x = node.x ?? 0
    const y = node.y ?? 0
    const r = node.radius
    const color = tone(node)
    const selected = node.id === selectedId

    if (node.active) {
      const pulse = reducedMotion ? 0.5 : 0.5 + 0.5 * Math.sin(performance.now() / 420 + x * 0.01)
      const halo = ctx.createRadialGradient(x, y, r * 0.6, x, y, r * (2.1 + pulse * 0.5))
      halo.addColorStop(0, alpha(color, 0.32))
      halo.addColorStop(1, alpha(color, 0))
      ctx.fillStyle = halo
      ctx.beginPath(); ctx.arc(x, y, r * (2.1 + pulse * 0.5), 0, Math.PI * 2); ctx.fill()
    }

    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2)
    ctx.fillStyle = node.isOwner ? color : palette.panel
    ctx.fill()
    ctx.lineWidth = selected ? 3 : node.active ? 2 : 1.2
    ctx.strokeStyle = selected ? palette.text : color
    if (node.kind === 'fold') ctx.setLineDash([3, 3])
    ctx.stroke()
    ctx.setLineDash([])

    // State pip (green working / yellow waiting / red blocked), shell status colors.
    if (node.kind !== 'fold' && !node.isOwner) {
      ctx.beginPath(); ctx.arc(x + r * 0.72, y - r * 0.72, Math.max(2.6, r * 0.24), 0, Math.PI * 2)
      ctx.fillStyle = stateColor(node); ctx.fill()
      ctx.lineWidth = 1.2; ctx.strokeStyle = palette.panel; ctx.stroke()
    }

    ctx.fillStyle = node.isOwner ? palette.panel : color
    ctx.font = `600 ${Math.round(r * 0.78)}px system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
    ctx.fillText(node.initials, x, y + 0.5)

    if (scale > 0.55 || node.active || selected || node.isOwner || node.kind === 'fold') {
      const size = Math.max(10, 11.5 / Math.max(scale, 0.8))
      ctx.font = `${node.active || selected ? 600 : 500} ${size}px system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`
      ctx.textBaseline = 'top'
      const text = [...node.label].length > 10 ? `${[...node.label].slice(0, 10).join('')}…` : node.label
      ctx.lineWidth = 3; ctx.strokeStyle = palette.panel
      ctx.strokeText(text, x, y + r + 4)
      ctx.fillStyle = node.active || selected ? palette.text : palette.muted
      ctx.fillText(text, x, y + r + 4)
    }
  }

  const paintPointer = (raw: GNode, color: string, ctx: CanvasRenderingContext2D) => {
    const node = raw as GNode & RelationNode
    ctx.fillStyle = color
    ctx.beginPath(); ctx.arc(node.x ?? 0, node.y ?? 0, node.radius + 4, 0, Math.PI * 2); ctx.fill()
  }

  const linkColor = (raw: GLink) => {
    const link = raw as GLink & RelationLink
    const base = link.human ? palette.yellow : palette.accent
    if (link.active) return alpha(base, 0.55)
    return link.kind === 'reports' ? alpha(palette.muted, 0.35) : alpha(base, 0.22)
  }

  const touchesSelected = (raw: GLink) => !!selectedId && (endpointId(raw.source) === selectedId || endpointId(raw.target) === selectedId)

  return (
    <div ref={hostRef} className="collab-relations" data-testid="collab-relations">
      {size.width > 0 && size.height > 0 && (
        <ForceGraph2D<RelationNode, RelationLink>
          ref={graphRef}
          graphData={graphData}
          width={size.width}
          height={size.height}
          backgroundColor="rgba(0,0,0,0)"
          nodeId="id"
          nodeLabel={() => ''}
          linkLabel={() => ''}
          nodeCanvasObject={paintNode}
          nodePointerAreaPaint={paintPointer}
          linkColor={linkColor}
          linkWidth={raw => ((raw as RelationLink).active ? 1.8 : 0.9) + (touchesSelected(raw) ? 0.8 : 0)}
          linkLineDash={raw => ((raw as RelationLink).active || (raw as RelationLink).kind === 'reports' ? null : [3, 4])}
          linkCurvature={raw => ((raw as RelationLink).kind === 'reports' ? 0 : 0.18)}
          linkDirectionalParticles={raw => (reducedMotion ? 0 : (raw as RelationLink).particles)}
          linkDirectionalParticleSpeed={raw => (raw as RelationLink).speed}
          linkDirectionalParticleWidth={raw => ((raw as RelationLink).kind === 'trace' ? 3.6 : 3)}
          linkDirectionalParticleColor={raw => ((raw as RelationLink).human ? palette.yellow : palette.accent)}
          autoPauseRedraw={reducedMotion}
          enableNodeDrag
          warmupTicks={reducedMotion ? 160 : 40}
          cooldownTicks={reducedMotion ? 0 : 240}
          d3VelocityDecay={0.32}
          minZoom={0.2}
          maxZoom={4}
          onNodeClick={raw => {
            const node = raw as GNode & RelationNode
            if (node.kind === 'fold') onExpandFold?.(String(node.id).replace(/^fold:/, ''))
            else onSelect?.(String(node.id))
          }}
          onNodeDragEnd={raw => {
            // A dragged node stays where it was put (visual only; the org is not changed).
            raw.fx = raw.x; raw.fy = raw.y
            userMoved.current = true
            remember()
          }}
          onEngineStop={() => {
            remember()
            if (!fitted.current && !userMoved.current) { fitted.current = true; graphRef.current?.zoomToFit(reducedMotion ? 0 : 400, 56) }
          }}
        />
      )}
      <ul className="collab-relations-legend" aria-label="关系图例">
        <li><i className="is-flow" aria-hidden="true" />协作中（粒子流动）</li>
        <li><i className="is-ai" aria-hidden="true" />AI</li>
        <li><i className="is-human" aria-hidden="true" />人类</li>
        <li><i className="is-dash" aria-hidden="true" />已采集关系</li>
      </ul>
    </div>
  )
}

export default RelationsGraph
