import { useState } from 'react'
import { editorFieldFor, healthLabel, type HealthItem } from '../workspace/workflowHealth'

const SHOWN = 4

/** Everything that would still refuse a save, in one line above the canvas. Each entry is a button (跳转编辑): it
 *  selects that step, opens its editor and puts the cursor in the control the finding is about -- a connector
 *  finding in the connector's own control, not the whole step -- so nobody has to open steps one by one to find
 *  what is wrong. */
export function WorkflowBlockerStrip({ items, zh, onFocus }: {
  items: readonly HealthItem[]
  zh: boolean
  onFocus: (nodeId: string, field: string) => void
}) {
  const [all, setAll] = useState(false)
  if (items.length === 0) return null
  const shown = all ? items : items.slice(0, SHOWN)
  return (
    <div className="wf-blocker-strip" role="region" aria-label={zh ? '还要补的地方' : 'What still needs filling in'}>
      <strong>{zh ? `还有 ${items.length} 处要补，保存才会通过` : `${items.length} thing${items.length === 1 ? '' : 's'} to fix before this saves`}</strong>
      <ul>
        {shown.map(item => {
          // A finding about the graph (a loop) points at the first step on it; with none, it is only text.
          const target = item.nodeId || item.nodes?.[0] || ''
          const field = editorFieldFor(item)
          const text = `${item.nodeName ? `${item.nodeName} · ` : ''}${healthLabel(item, zh)}`
          return (
            <li key={`${item.nodeId}|${item.field}|${item.code}|${item.dependency ?? ''}`}>
              {target
                ? <button type="button" className="wf-blocker-item" data-jump={field} title={item.message || undefined} onClick={() => onFocus(target, field)}>{text}</button>
                : <span className="wf-blocker-item is-text" title={item.message || undefined}>{text}</span>}
            </li>
          )
        })}
        {items.length > SHOWN && (
          <li>
            <button type="button" className="wf-blocker-more" aria-expanded={all} onClick={() => setAll(value => !value)}>
              {all ? (zh ? '收起' : 'Show fewer') : (zh ? `另有 ${items.length - SHOWN} 处` : `${items.length - SHOWN} more`)}
            </button>
          </li>
        )}
      </ul>
    </div>
  )
}
