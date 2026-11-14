import { useMemo, type ReactElement } from 'react'
import type { ProgressEntry } from '../types/kanban'
import { progressEntryKey } from '../lib/progressEntryKey'

/** FE-EB-21：工具块步骤条（自研缩略条，不引入对方组件）。 */
export type ToolStepStripProps = {
  entries: readonly ProgressEntry[]
  /** 最多展示的步骤芯片；更早的折叠为 +N。 */
  maxVisible?: number
  zh?: boolean
  activeTool?: string | null
  onSelectStep?: (entry: ProgressEntry, index: number) => void
}

export type ToolStepChip = {
  key: string
  index: number
  kind: 'tool' | 'think' | 'verify' | 'other'
  label: string
  entry: ProgressEntry
}

export function buildToolStepChips(entries: readonly ProgressEntry[]): ToolStepChip[] {
  const chips: ToolStepChip[] = []
  entries.forEach((entry, index) => {
    if (entry.type === 'tool_call') {
      chips.push({ key: progressEntryKey(entry), index, kind: 'tool', label: String(entry.summary || 'tool').trim() || 'tool', entry })
      return
    }
    if (entry.type === 'thinking') {
      chips.push({ key: progressEntryKey(entry), index, kind: 'think', label: '…', entry })
      return
    }
    if (entry.type === 'verification') {
      chips.push({ key: progressEntryKey(entry), index, kind: 'verify', label: String(entry.summary || 'check').trim() || 'check', entry })
    }
  })
  return chips
}

function shortLabel(label: string, max = 18): string {
  const text = label.trim()
  if (text.length <= max) return text
  return `${text.slice(0, max - 1)}…`
}

export function ToolStepStrip(props: ToolStepStripProps): ReactElement | null {
  const { entries, maxVisible = 10, zh = false, activeTool, onSelectStep } = props
  const chips = useMemo(() => buildToolStepChips(entries), [entries])
  if (!chips.length) return null

  const overflow = chips.length > maxVisible ? chips.length - maxVisible : 0
  const visible = overflow > 0 ? chips.slice(-maxVisible) : chips

  return (
    <div className="ptl-step-strip" data-testid="ptl-step-strip" role="list" aria-label={zh ? '工具步骤条' : 'Tool step strip'}>
      {overflow > 0 && (
        <span className="ptl-step-chip ptl-step-overflow" data-testid="ptl-step-overflow" role="listitem">
          +{overflow}
        </span>
      )}
      {visible.map((chip, i) => {
        const isLast = i === visible.length - 1
        const active = Boolean(activeTool && chip.kind === 'tool' && chip.label === activeTool)
        const body = (
          <>
            <span className="ptl-step-thumb" data-kind={chip.kind} aria-hidden>
              {chip.kind === 'tool' ? '⚒' : chip.kind === 'think' ? '…' : chip.kind === 'verify' ? '✓' : '·'}
            </span>
            <span className="ptl-step-label">{shortLabel(chip.label)}</span>
          </>
        )
        return (
          <span key={chip.key} className="ptl-step-item" role="listitem">
            {onSelectStep ? (
              <button
                type="button"
                className={`ptl-step-chip${active ? ' is-active' : ''}${isLast ? ' is-latest' : ''}`}
                data-testid="ptl-step-chip"
                data-kind={chip.kind}
                data-step-index={chip.index}
                onClick={() => onSelectStep(chip.entry, chip.index)}
              >
                {body}
              </button>
            ) : (
              <span
                className={`ptl-step-chip${active ? ' is-active' : ''}${isLast ? ' is-latest' : ''}`}
                data-testid="ptl-step-chip"
                data-kind={chip.kind}
                data-step-index={chip.index}
              >
                {body}
              </span>
            )}
            {!isLast && <span className="ptl-step-rail" aria-hidden />}
          </span>
        )
      })}
    </div>
  )
}
