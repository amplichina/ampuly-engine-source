// 关系 view host: theme switcher (视界熔炉 / 天工开物 / 数据飞轮, persisted in localStorage,
// T cycles), honest automatic fallbacks to the internal basic force graph (no WebGL /
// reduced motion / theme crashed or chunk failed to load; never user-selectable), and one
// lazy chunk per theme so three.js and each renderer only load when that theme is chosen.
import { Component, Suspense, lazy, useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import type { RelationGraphData, RelationPulse } from './relations'
import {
  BASIC_VIEW_LABEL, THEMES, buildThemeModel, isThemeCycleKey, nextTheme, readStoredTheme, resolveTheme, storeTheme,
  type RelationFallback, type RelationTheme,
} from './themeModel'
import { hasWebGL, markWebGLUnavailable } from './webgl'

const ForgeTheme = lazy(() => import('./themes/ForgeTheme'))
const WorksTheme = lazy(() => import('./themes/WorksTheme'))
const OpsTheme = lazy(() => import('./themes/OpsTheme'))
const RelationsGraph = lazy(() => import('./RelationsGraph'))

export interface RelationsStageProps {
  data: RelationGraphData
  pulses: RelationPulse[]
  taskTitles: Readonly<Record<string, string>>
  selectedId: string
  reducedMotion: boolean
  onSelect: (id: string) => void
  onExpandFold: (managerId: string) => void
  /** Test hook: initial theme without touching storage. */
  initialTheme?: RelationTheme
  /** Test hook: pretend this theme already failed to render. */
  initialFailedTheme?: RelationTheme
}

/** Catches a theme's render error or lazy-chunk load failure so the stage can fall back. */
class ThemeErrorBoundary extends Component<{ onError: () => void; children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch() { this.props.onError() }
  render() { return this.state.failed ? null : this.props.children }
}

export function fallbackMessage(fallback: RelationFallback, themeLabel: string): string {
  if (fallback === 'webgl') return `此环境不支持 WebGL，已自动改用${BASIC_VIEW_LABEL}`
  if (fallback === 'motion') return `已开启「减少动态效果」，以静态${BASIC_VIEW_LABEL}显示`
  return `「${themeLabel}」渲染出错，已自动改用${BASIC_VIEW_LABEL}（切换主题可重试）`
}

export function RelationsStage({ data, pulses, taskTitles, selectedId, reducedMotion, onSelect, onExpandFold, initialTheme, initialFailedTheme }: RelationsStageProps) {
  const [preferred, setPreferred] = useState<RelationTheme>(() => initialTheme ?? readStoredTheme())
  const [webgl, setWebgl] = useState(true)
  const [failed, setFailed] = useState<RelationTheme | undefined>(initialFailedTheme)
  const [expanded, setExpanded] = useState(false)
  useEffect(() => {
    if (!expanded) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setExpanded(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expanded])
  useEffect(() => { setWebgl(hasWebGL()) }, [])
  // Choosing (or cycling to) a theme clears an earlier failure so it gets one more try.
  const choose = useCallback((theme: RelationTheme) => { setPreferred(theme); setFailed(undefined); storeTheme(theme) }, [])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (!isThemeCycleKey(event, target?.tagName, target?.isContentEditable)) return
      event.preventDefault()
      setFailed(undefined)
      setPreferred(current => { const next = nextTheme(current); storeTheme(next); return next })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const { theme, fallback } = resolveTheme(preferred, { webgl, reducedMotion, failed })
  const model = useMemo(() => buildThemeModel(data, taskTitles, pulses), [data, taskTitles, pulses])
  const unsupported = useCallback(() => { markWebGLUnavailable(); setWebgl(false) }, [])
  const crashed = useCallback(() => setFailed(preferred), [preferred])
  const common = { data, model, selectedId, reducedMotion, pulses, onSelect, onExpandFold, onUnsupported: unsupported }
  const preferredLabel = THEMES.find(info => info.id === preferred)?.label ?? ''
  const loadingLabel = theme === 'basic' ? BASIC_VIEW_LABEL : THEMES.find(info => info.id === theme)?.label

  return (
    <div className={`collab-relations-stage${expanded ? ' is-expanded' : ''}`} data-theme={theme} data-preferred={preferred}>
      <div className="collab-theme-switch" role="radiogroup" aria-label="关系视图主题（按 T 切换）">
        {THEMES.map(info => (
          <button key={info.id} type="button" role="radio" aria-checked={preferred === info.id}
            className={preferred === info.id ? 'is-active' : ''} onClick={() => choose(info.id)}
            title={`${info.en}${info.webgl ? ' · WebGL' : ''} · 按 T 循环切换`}>
            {info.label}
          </button>
        ))}
        <button type="button" className="collab-theme-expand" aria-pressed={expanded} onClick={() => setExpanded(value => !value)}
          title={expanded ? '退出全屏（Esc）' : '全屏展示关系视图'}>
          {expanded ? '退出全屏' : '全屏'}
        </button>
      </div>
      {fallback && (
        <span className="collab-graph-chip is-warn collab-graph-fallback" role="status">
          {fallbackMessage(fallback, preferredLabel)}
        </span>
      )}
      <Suspense fallback={<div className="collab-graph-loading is-space">正在载入「{loadingLabel}」…</div>}>
        {theme === 'basic'
          ? <RelationsGraph data={data} selectedId={selectedId} reducedMotion={reducedMotion} onSelect={onSelect} onExpandFold={onExpandFold} />
          : (
            <ThemeErrorBoundary key={theme} onError={crashed}>
              <Suspense fallback={<div className="collab-graph-loading is-space">正在载入「{loadingLabel}」…</div>}>
                {theme === 'forge' ? <ForgeTheme {...common} />
                  : theme === 'works' ? <WorksTheme {...common} />
                    : <OpsTheme {...common} />}
              </Suspense>
            </ThemeErrorBoundary>
          )}
      </Suspense>
    </div>
  )
}
