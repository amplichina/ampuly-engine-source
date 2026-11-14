import { useEffect, useState } from 'react'

/** Measured animation frame rate (updates once per second); null when not animating. */
export function useFps(enabled: boolean): number | null {
  const [fps, setFps] = useState<number | null>(null)
  useEffect(() => {
    if (!enabled || typeof requestAnimationFrame === 'undefined') { setFps(null); return }
    let frames = 0
    let start = performance.now()
    let handle = 0
    const tick = (now: number) => {
      frames += 1
      if (now - start >= 1000) {
        setFps(Math.round((frames * 1000) / (now - start)))
        frames = 0
        start = now
      }
      handle = requestAnimationFrame(tick)
    }
    handle = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(handle)
  }, [enabled])
  return fps
}
