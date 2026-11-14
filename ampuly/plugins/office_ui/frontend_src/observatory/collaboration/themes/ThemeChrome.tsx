// Shared chrome for the theme views: title block, honest-data note, real stat strip,
// trace record list and the measured frame rate. Original Ampuly code.
import type { ReactNode } from 'react'

import type { ThemeModel } from '../themeModel'
import { clockText, memberName } from './types'

export function ThemeTitle({ en, zh, tagline }: { en: string; zh: string; tagline: string }) {
  const [first, ...rest] = en.split(' ')
  return (
    <div className="collab-theme-title">
      <b>{first}<br />{rest.join(' ')} <em>{zh}</em></b>
      <span>{tagline}</span>
    </div>
  )
}

/** Clearly marks what is live data and what is decorative motion. */
export function SimNote({ children }: { children?: ReactNode }) {
  return (
    <p className="collab-theme-simnote">
      <i aria-hidden="true" />{children ?? '名单、状态、任务与轨迹为实时数据 · 粒子 / 织造 / 旋转为演示动效（非实时数据）'}
    </p>
  )
}

export function StatStrip({ model, fps, extra }: { model: ThemeModel; fps: number | null; extra?: [string, string | number][] }) {
  const rows: [string, string | number][] = [
    ['成员 MEMBERS', model.members.length + model.hiddenMembers],
    ['在办 ACTIVE', model.activeMembers],
    ['任务 TASKS', model.tasks.length],
    ['阻塞 BLOCKED', model.blocked],
    ['待你 WAITING', model.waiting],
    ...(extra ?? []),
    ['帧率 FPS', fps === null ? '—' : fps],
  ]
  return (
    <dl className="collab-theme-stats">
      {rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
    </dl>
  )
}

export function TraceRecords({ model, title, empty = '暂无轨迹事件' }: { model: ThemeModel; title: string; empty?: string }) {
  const rows = model.events.slice(-7).reverse()
  return (
    <section className="collab-theme-panel collab-theme-records">
      <header><span>{title}</span><span>{model.events.length} 条 · 实时</span></header>
      {rows.length ? (
        <ol>
          {rows.map(event => (
            <li key={event.id}>
              <time>{clockText(event.time)}</time>
              <b>{memberName(model, event.actorId)}</b>
              <span>{event.title || event.type || '事件'}</span>
            </li>
          ))}
        </ol>
      ) : <p className="collab-theme-empty">{empty}</p>}
    </section>
  )
}
