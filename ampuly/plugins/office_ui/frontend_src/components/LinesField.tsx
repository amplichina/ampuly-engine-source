import { useEffect, useRef, useState } from 'react'

/** One entry per line: the text a person typed becomes a list without the field rewriting what they see. */
export function linesFrom(text: string): string[] {
  return text.split(/\r?\n/).map(item => item.trim()).filter(Boolean)
}

/**
 * A list edited as text, one entry per line.
 *
 * The earlier comma-separated inputs were controlled by the parsed list joined back with ", ": typing a comma
 * produced the same list, so the comma never appeared and a second entry could only be pasted; and an entry that
 * itself contains a comma ("客户名称，行业与风格") was split the first time the field was touched. Here the person's
 * text is kept as typed while the field has the focus, entries may contain commas, and the list is parsed from it.
 */
export function LinesField({ value, onChange, field, placeholder, rows = 2 }: {
  value: readonly string[]
  onChange: (next: string[]) => void
  /** data-field: what the blocker strip puts the cursor in. */
  field: string
  placeholder?: string
  rows?: number
}) {
  const [text, setText] = useState(() => value.join('\n'))
  const focused = useRef(false)
  // From outside (undo, a proposal, another step opened): show it -- unless the person is typing here, in which case
  // their text is already what produced `value`.
  const joined = value.join('\n')
  useEffect(() => {
    if (!focused.current || linesFrom(text).join('\n') !== joined) setText(joined)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [joined])
  return (
    <textarea
      data-field={field}
      rows={Math.max(rows, Math.min(6, value.length + 1))}
      value={text}
      placeholder={placeholder}
      onFocus={() => { focused.current = true }}
      onBlur={() => { focused.current = false; setText(linesFrom(text).join('\n')) }}
      onChange={event => { setText(event.target.value); onChange(linesFrom(event.target.value)) }}
    />
  )
}
