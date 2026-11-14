// Shared 2D canvas loop for the canvas themes: DPR-aware sizing, a single rAF loop that
// pauses when the tab is hidden, a static single frame under reduced motion, and simple
// circular hit-testing for clicks. Original Ampuly code.
import { useEffect, useRef } from 'react'

export interface Hit { id: string; x: number; y: number; r: number }

export interface StageFrame {
  ctx: CanvasRenderingContext2D
  w: number
  h: number
  /** Seconds since mount (frozen at a pleasant value when static). */
  t: number
  dt: number
  hits: Hit[]
}

export function useCanvasStage(
  draw: (frame: StageFrame) => void,
  options: { animate: boolean; maxDpr?: number; onHit?: (id: string) => void; deps?: readonly unknown[] },
) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const drawRef = useRef(draw)
  drawRef.current = draw
  const hitsRef = useRef<Hit[]>([])
  const onHitRef = useRef(options.onHit)
  onHitRef.current = options.onHit
  const { animate, maxDpr = 1.75 } = options

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    let w = 0
    let h = 0
    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      const dpr = Math.min(maxDpr, window.devicePixelRatio || 1)
      w = Math.max(1, rect.width)
      h = Math.max(1, rect.height)
      canvas.width = Math.round(w * dpr)
      canvas.height = Math.round(h * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()
    const start = performance.now()
    let last = start
    let handle = 0
    const frame = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000)
      last = now
      const hits: Hit[] = []
      drawRef.current({ ctx, w, h, t: animate ? (now - start) / 1000 : 12, dt: animate ? dt : 0, hits })
      hitsRef.current = hits
      if (animate) handle = requestAnimationFrame(frame)
    }
    const observer = typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(() => { resize(); if (!animate) frame(performance.now()) })
      : undefined
    observer?.observe(canvas)
    handle = requestAnimationFrame(frame)
    const onClick = (event: MouseEvent) => {
      const rect = canvas.getBoundingClientRect()
      const x = event.clientX - rect.left
      const y = event.clientY - rect.top
      let best: Hit | undefined
      let bestD = Infinity
      for (const hit of hitsRef.current) {
        const d = Math.hypot(hit.x - x, hit.y - y)
        if (d <= hit.r && d < bestD) { best = hit; bestD = d }
      }
      if (best) onHitRef.current?.(best.id)
    }
    const onMove = (event: MouseEvent) => {
      const rect = canvas.getBoundingClientRect()
      const x = event.clientX - rect.left
      const y = event.clientY - rect.top
      canvas.style.cursor = hitsRef.current.some(hit => Math.hypot(hit.x - x, hit.y - y) <= hit.r) ? 'pointer' : ''
    }
    canvas.addEventListener('click', onClick)
    canvas.addEventListener('mousemove', onMove)
    return () => {
      cancelAnimationFrame(handle)
      observer?.disconnect()
      canvas.removeEventListener('click', onClick)
      canvas.removeEventListener('mousemove', onMove)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [animate, maxDpr, ...(options.deps ?? [])])

  return canvasRef
}
