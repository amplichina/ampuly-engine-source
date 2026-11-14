// WebGL capability probe for the 视界熔炉 theme; without WebGL the 关系 view falls back to the internal basic graph.
let cached: boolean | undefined

export function hasWebGL(): boolean {
  if (cached !== undefined) return cached
  if (typeof document === 'undefined') return false
  try {
    const canvas = document.createElement('canvas')
    const gl = canvas.getContext('webgl2') ?? canvas.getContext('webgl')
    cached = !!gl
    ;(gl as WebGLRenderingContext | null)?.getExtension('WEBGL_lose_context')?.loseContext()
  } catch {
    cached = false
  }
  return cached
}

export function markWebGLUnavailable(): void { cached = false }
