/** FE-EB-13/12：从 progress/tool_call 行解析可跳转的浏览器 binding 或可预览产物路径。无元数据则返回 null，勿造假按钮。 */
import type { ProgressEntry } from '../types/kanban'

const BROWSER_TOOL_PREFIXES = [
  'browser_',
  'browser.',
]

export type ArtifactPathLike = { path: string }

export function parseProgressDetailObject(detail: string | undefined): Record<string, unknown> | null {
  if (!detail) return null
  const trimmed = detail.trim()
  if (!trimmed) return null
  // detail 可能是纯 JSON，或 JSON 后跟完成摘要；取第一个完整对象。
  const start = trimmed.indexOf('{')
  if (start < 0) return null
  let depth = 0
  let inString = false
  let escape = false
  for (let i = start; i < trimmed.length; i += 1) {
    const ch = trimmed[i]
    if (inString) {
      if (escape) escape = false
      else if (ch === '\\') escape = true
      else if (ch === '"') inString = false
      continue
    }
    if (ch === '"') { inString = true; continue }
    if (ch === '{') depth += 1
    else if (ch === '}') {
      depth -= 1
      if (depth === 0) {
        try {
          const parsed = JSON.parse(trimmed.slice(start, i + 1)) as unknown
          if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            return parsed as Record<string, unknown>
          }
          return null
        } catch {
          return null
        }
      }
    }
  }
  return null
}

function stringField(obj: Record<string, unknown> | null, ...keys: string[]): string | null {
  if (!obj) return null
  for (const key of keys) {
    const value = obj[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return null
}

/** 显式 browser binding 元数据（detail JSON）。 */
export function extractProgressBindingKey(entry: ProgressEntry | null | undefined): string | null {
  if (!entry) return null
  const obj = parseProgressDetailObject(entry.detail)
  return stringField(obj, 'binding_key', 'bindingKey', 'browser_binding_key', 'browserBindingKey')
}

export function isBrowserToolSummary(summary: string | undefined): boolean {
  const name = String(summary ?? '').trim().toLowerCase()
  if (!name) return false
  return BROWSER_TOOL_PREFIXES.some(prefix => name.startsWith(prefix))
}

/**
 * 解析「定位到画面」目标：
 * 1) detail 里有 binding_key → 用之；
 * 2) 否则若是 browser_* 工具且调用方提供了关联 live binding → 用之；
 * 3) 否则 null（不显示按钮）。
 */
export function resolveProgressBrowserBinding(
  entry: ProgressEntry | null | undefined,
  correlatedBindingKey?: string | null,
): string | null {
  const explicit = extractProgressBindingKey(entry)
  if (explicit) return explicit
  if (entry && isBrowserToolSummary(entry.summary) && correlatedBindingKey) {
    return correlatedBindingKey
  }
  return null
}

/** 工具行里的文件路径候选（file_read/write 等）。 */
export function extractProgressArtifactPath(entry: ProgressEntry | null | undefined): string | null {
  if (!entry) return null
  const obj = parseProgressDetailObject(entry.detail)
  return stringField(obj, 'path', 'file', 'filepath', 'file_path', 'artifact_path', 'artifactPath')
}

function normalizedArtifactPath(value: string): string | null {
  if (typeof value !== 'string') return null
  const path = value.trim().replace(/\\/g, '/').replace(/^(?:\.\/)+/, '')
  if (!path || /[\u0000-\u001f\u007f]/.test(path) || /^[a-z][a-z0-9+.-]*:\/\//i.test(path)) return null
  if (path.split('/').some(part => part === '.' || part === '..')) return null
  return path
}

/** Exact indexed path first; a shortened path must identify one unique artifact. */
export function matchArtifactIndexPath(
  candidate: string | null | undefined,
  files: readonly ArtifactPathLike[] | null | undefined,
): string | null {
  const want = normalizedArtifactPath(candidate ?? '')
  if (!want || !files || files.length === 0) return null
  const indexed = files.map(file => ({ file, path: normalizedArtifactPath(file.path) }))
    .filter((item): item is { file: ArtifactPathLike; path: string } => item.path !== null)
  const exact = indexed.find(item => item.path === want)
  if (exact) return exact.file.path
  const matches = indexed.filter(({ path }) => {
    return path.endsWith('/' + want) || want.endsWith('/' + path)
  })
  // Two orders can both produce report.md. Index order must never choose the order.
  return new Set(matches.map(item => item.path)).size === 1 ? matches[0].file.path : null
}
