// The builder's own step card (frontend refactor U2-4). The run view keeps RunGraphNode; the builder needs things
// that card has no place for -- a caption saying whose step it is and who confirms it, a grip -- so it gets its own
// component under the same React Flow type key ('runNode'): every selector, hit test and saved position that names
// that key keeps working. The rows, their drop slots and the two ports are the run card's, unchanged.

import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'

import { NODE_PADDING, NODE_ROW_HEIGHT, type RunGraphNodeType } from './RunGraphNode'

export const BUILDER_HEADER_HEIGHT = 40
export const BUILDER_CAPTION_HEIGHT = 22
/** The in-place 职责 field of an editable card: label-less, two lines, with its margins. */
export const BUILDER_FIELD_HEIGHT = 58

/** The height the lane layout reserves for a builder card with this many rows. Must match the stylesheet. */
export function builderCardHeight(rowCount: number, editable = false): number {
  return BUILDER_HEADER_HEIGHT + BUILDER_CAPTION_HEIGHT + (editable ? BUILDER_FIELD_HEIGHT : 0)
    + NODE_PADDING * 2 + Math.max(0, rowCount) * NODE_ROW_HEIGHT
}

/** What an editable card carries. `commit` takes the whole new value when the person is done with the field (blur,
 *  or Enter in the name) -- never per keystroke: every patch re-seeds the canvas. `begin` is called once when a field
 *  gets the focus, so the whole edit is one undo step. */
export type CardEdit = {
  name: string
  responsibility: string
  begin: () => void
  commit: (field: 'name' | 'responsibility', value: string) => void
  labels: { name: string; responsibility: string; placeholder: string }
}

/** What a finished edit should write, or null when there is nothing to write. A name cannot be emptied from the card:
 *  an unnamed step would show its id, which is not what the person typed. */
export function committedValue(field: 'name' | 'responsibility', before: string, typed: string): string | null {
  const value = field === 'name' ? typed.trim() : typed.replace(/\s+$/, '')
  if (field === 'name' && !value) return null
  return value === before ? null : value
}

function CardField({ field, edit, multiline }: { field: 'name' | 'responsibility'; edit: CardEdit; multiline?: boolean }) {
  const saved = edit[field]
  const [typed, setTyped] = useState(saved)
  const focused = useRef(false)
  // What is saved shows again whenever the person is not in the middle of typing here (undo, the inspector, a proposal).
  useEffect(() => { if (!focused.current) setTyped(saved) }, [saved])
  const done = () => {
    focused.current = false
    const value = committedValue(field, saved, typed)
    if (value === null) setTyped(saved)
    else edit.commit(field, value)
  }
  // A card leaves the DOM mid-edit on a big flow (only the cards in view are rendered, and a wheel zoom or a minimap
  // pan can move it out) -- and no blur comes: what was typed is committed rather than dropped.
  const doneRef = useRef(done)
  doneRef.current = done
  useEffect(() => () => { if (focused.current) doneRef.current() }, [])
  const shared = {
    // React Flow: not a drag handle, not a pan surface, and the wheel scrolls the text.
    className: `wf-card-field wf-card-field--${field} nodrag nopan nowheel`,
    'data-card-field': field,
    'aria-label': edit.labels[field],
    value: typed,
    onFocus: () => { focused.current = true; edit.begin() },
    onBlur: done,
    // Keys typed here are text: Backspace must not remove a wire, Ctrl+Z is the field's own undo.
    onKeyDown: (event: { key: string; stopPropagation: () => void; currentTarget: { blur: () => void } }) => {
      event.stopPropagation()
      if (event.key === 'Escape') { setTyped(saved); focused.current = false; event.currentTarget.blur() }
      if (event.key === 'Enter' && !multiline) event.currentTarget.blur()
    },
  }
  return multiline
    ? <textarea {...shared} rows={2} placeholder={edit.labels.placeholder} onChange={event => setTyped(event.target.value)} />
    : <input {...shared} onChange={event => setTyped(event.target.value)} />
}

/** Whose step this is and, for a gated one, who confirms it. A step with no role says so: that is what to fix. */
export function cardCaption(step: { roleLabel: string; gated: boolean; signer: string }, zh: boolean): string {
  const role = step.roleLabel.trim() || (zh ? '待选角色' : 'no role yet')
  if (!step.gated) return role
  const signer = step.signer.trim() || 'owner'
  return zh ? `${role} · 完成后交 ${signer} 确认` : `${role} · then ${signer} confirms`
}

export function BuilderNode({ data }: NodeProps<RunGraphNodeType>) {
  const classes = ['rg-node', 'wf-card', `rg-tone-${data.tone}`]
  const caption = typeof data.caption === 'string' ? data.caption : ''
  const edit = (data.edit ?? null) as CardEdit | null
  // 语义色 over the role tint (stepSemantics): what kind of work this is, as a strip across the top.
  const semantic = typeof data.semantic === 'string' ? data.semantic : ''
  const semanticLabel = typeof data.semanticLabel === 'string' ? data.semanticLabel : ''
  return (
    <div className={classes.join(' ')} style={{ '--role': data.roleColor } as CSSProperties}>
      {semantic && <span className="wf-card-sem" data-sem={semantic} title={semanticLabel} aria-label={semanticLabel} />}
      <Handle type="target" position={Position.Left} className="rg-port rg-port-in" />
      <div className="rg-node-header wf-card-head">
        <span className="rg-node-kind">{data.kindLabel}</span>
        {edit
          ? <CardField field="name" edit={edit} />
          : <span className="rg-node-title" title={data.title}>{data.title}</span>}
        <span className="wf-card-grip" aria-hidden="true">⋮⋮</span>
      </div>
      <div className="wf-card-caption" title={caption}>{caption}</div>
      {edit && <div className="wf-card-fields"><CardField field="responsibility" edit={edit} multiline /></div>}
      <div className="rg-node-body">
        {data.rows.map(row => (
          <div className="rg-row" key={row.label} data-slot={row.slot}>
            <span className="rg-row-label">{row.label}</span>
            <span className={`rg-row-value${row.tone ? ` rg-tone-${row.tone}` : ''}`} title={row.title ?? row.value}>{row.value}</span>
          </div>
        ))}
      </div>
      <Handle type="source" position={Position.Right} className="rg-port rg-port-out" />
      {/* 分支 / 汇合 (flowShape): on the lower corners, outside the card's height, so the lane layout is unchanged. */}
      {shapeMark(data.joinMark) && <ShapeMark kind="join" mark={shapeMark(data.joinMark)!} />}
      {shapeMark(data.splitMark) && <ShapeMark kind="split" mark={shapeMark(data.splitMark)!} />}
    </div>
  )
}

type Mark = { label: string; title: string }

function shapeMark(value: unknown): Mark | null {
  const mark = value as Partial<Mark> | null | undefined
  return mark && typeof mark.label === 'string' && typeof mark.title === 'string' ? { label: mark.label, title: mark.title } : null
}

function ShapeMark({ kind, mark }: { kind: 'join' | 'split'; mark: Mark }) {
  return <span className={`wf-card-shape wf-card-shape--${kind}`} data-shape={kind} title={mark.title} aria-label={mark.title}>{mark.label}</span>
}
