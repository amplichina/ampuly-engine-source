// The product shell's icons. The side rail's five primary entries carry the interaction prototype's glyphs exactly
// (policy: 「侧边的 icon 严格按照交互构型」) -- as text, in a span; the two foot entries keep an SVG with the
// prototype's own paths (preferences.js prefIcon: 21×21, stroke 1.7, round caps). This supersedes the earlier note
// that the prototype's glyphs were not carried over: they are, and the self-drawn primary icons are gone. The top
// bar's icons (执行区 / 待审批 / 更多) are untouched in this batch.

import type { RailPage } from '../../lib/shellNav'

/** The prototype's glyph for each primary entry, by code point. */
export const RAIL_GLYPHS = {
  master: '✧',      // ✧ 总控
  workflows: '⌘',   // ⌘ 搭建台
  dashboard: '▦',   // ▦ 项目中心
  orders: '☷',      // ☷ 业务工单
  components: '⊞',  // ⊞ 组件库
} as const satisfies Partial<Record<RailPage, string>>

type GlyphPage = keyof typeof RAIL_GLYPHS

const PATHS: Record<'more' | 'approvals' | 'execution', string[]> = {
  // 执行区: a screen with a cursor -- watching, and when asked, taking over.
  execution: ['M3.5 5h17v11h-17z', 'M9 20h6', 'M12 16v4', 'M10 8.5l4.5 2.2-2 .7-.8 2z'],
  more: ['M5 12h.01', 'M12 12h.01', 'M19 12h.01'],
  approvals: ['M5 4.5h14v15H5z', 'M8.5 12.2l2.4 2.4 4.8-5'],
}

function isGlyph(name: string): name is GlyphPage {
  return Object.prototype.hasOwnProperty.call(RAIL_GLYPHS, name)
}

/** The foot entries, drawn from the prototype's paths: 配置 two sliders, 设置 the gear. */
function FootIcon({ name }: { name: 'config' | 'settings' }) {
  return (
    <svg className="shell-rail-svg" width={21} height={21} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {name === 'config' ? (
        <>
          <path d="M4 7h16M4 17h16" />
          <circle cx="8" cy="7" r="3" fill="var(--surface)" />
          <circle cx="16" cy="17" r="3" fill="var(--surface)" />
        </>
      ) : (
        <>
          <path d="m9 3-.5 2-2 .9-1.8-.6-2 3.4 1.5 1.4v2.3L2.7 14l2 3.4 1.8-.6 2 1L9 20h4l.5-2.2 2-1 1.8.6 2-3.4-1.5-1.6v-2.3l1.5-1.4-2-3.4-1.8.6-2-.9L13 3Z" transform="translate(1 0)" />
          <circle cx="12" cy="11.5" r="3" />
        </>
      )}
    </svg>
  )
}

export function ShellIcon({ name, size = 20 }: { name: RailPage | keyof typeof PATHS; size?: number }) {
  if (isGlyph(name)) return <span className="shell-rail-glyph" aria-hidden="true">{RAIL_GLYPHS[name]}</span>
  if (name === 'config' || name === 'settings') return <FootIcon name={name} />
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={name === 'more' ? 2.6 : 1.6}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {PATHS[name].map(d => <path key={d} d={d} />)}
    </svg>
  )
}
