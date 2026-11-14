/** One live-view input bridge. Nothing is persisted, logged, retried or replayed. */
export type BrowserInput = Record<string, unknown>
export type InputResult = { events: BrowserInput[]; preventDefault: boolean; error?: 'text_too_long' | 'invalid_text' }
type Key = { key: string; code?: string; ctrlKey?: boolean; altKey?: boolean; metaKey?: boolean; shiftKey?: boolean;
  isComposing?: boolean; keyCode?: number; altGraph?: boolean }
type Scope = { project_id?: string; binding_key?: string; resource_id?: string; control_fence?: string }
const idle = (): InputResult => ({ events: [], preventDefault: false })
const modifierKeys = new Set(['Alt', 'Control', 'Meta', 'Shift', 'AltGraph', 'CapsLock'])

export function browserInputScope(frame: Scope): string | null {
  const parts = [frame.project_id, frame.binding_key, frame.resource_id, frame.control_fence]
  return parts.every(value => typeof value === 'string' && value.length > 0 && value.length <= 256
    && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value)) ? JSON.stringify(parts) : null
}

export function browserKeyModifiers(event: Key): number {
  return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0)
}

export function browserPoint(clientX: number, clientY: number, rect: { left: number; top: number; width: number; height: number }, width: number, height: number): { x: number; y: number } | null {
  if (![clientX, clientY, rect.left, rect.top, rect.width, rect.height, width, height].every(Number.isFinite)
    || rect.width <= 0 || rect.height <= 0 || width <= 0 || height <= 0
    || clientX < rect.left || clientY < rect.top || clientX > rect.left + rect.width || clientY > rect.top + rect.height) return null
  return { x: Math.min(width - 1, Math.max(0, (clientX - rect.left) / rect.width * width)),
    y: Math.min(height - 1, Math.max(0, (clientY - rect.top) / rect.height * height)) }
}

export function browserWheel(deltaX: number, deltaY: number, deltaMode: number, width: number, height: number): { delta_x: number; delta_y: number } | null {
  if (![deltaX, deltaY, width, height].every(Number.isFinite) || width <= 0 || height <= 0 || ![0, 1, 2].includes(deltaMode)) return null
  const x = deltaX * (deltaMode === 1 ? 16 : deltaMode === 2 ? width : 1)
  const y = deltaY * (deltaMode === 1 ? 16 : deltaMode === 2 ? height : 1)
  if (!x && !y) return null
  return { delta_x: Math.max(-4096, Math.min(4096, x)), delta_y: Math.max(-4096, Math.min(4096, y)) }
}

export class BrowserLiveInput {
  private scope: string | null = null
  private enabled = false
  private composing = false
  private discardedComposition = false
  private committedText: string | null = null
  private pressed = new Map<string, { key: string; code?: string }>()

  configure(scope: string | null, enabled: boolean): void {
    if (scope === this.scope && enabled === this.enabled) return
    this.discardedComposition = this.discardedComposition || this.composing
    this.scope = scope
    this.enabled = enabled && scope !== null
    this.composing = false
    this.committedText = null
    this.pressed.clear()
  }

  accepts(scope: string | null): boolean { return this.enabled && scope !== null && scope === this.scope }
  isComposing(scope: string | null): boolean { return this.accepts(scope) && this.composing }

  compositionStart(scope: string | null): void {
    if (!this.accepts(scope)) return
    this.composing = true
    this.discardedComposition = false
    this.committedText = null
  }

  compositionEnd(scope: string | null, text: string): InputResult {
    if (!this.accepts(scope) || !this.composing) return idle()
    this.composing = false
    // Some browsers emit another input with the same value after compositionend.
    // A later deliberate key/paste/composition clears this marker, so repeated words still work.
    this.committedText = text.length <= 1024 ? text : null
    return this.commit(text)
  }

  input(scope: string | null, text: string, isComposing = false): InputResult {
    if (!this.accepts(scope) || this.composing || isComposing || this.discardedComposition) return idle()
    if (this.committedText !== null) {
      const duplicate = text === this.committedText
      this.committedText = null
      if (duplicate) return idle()
    }
    return this.commit(text)
  }

  paste(scope: string | null, text: string): InputResult {
    if (!this.accepts(scope)) return idle()
    // Pasting while an IME candidate is live could commit two different pieces of text.
    if (this.composing) return { events: [], preventDefault: true }
    this.discardedComposition = false
    this.committedText = text.length <= 1024 ? text : null
    return { ...this.commit(text), preventDefault: true }
  }

  key(scope: string | null, type: 'keyDown' | 'keyUp', event: Key): InputResult {
    if (!this.accepts(scope)) return idle()
    const id = event.code || event.key
    if (type === 'keyUp') {
      const pressed = this.pressed.get(id)
      this.pressed.delete(id)
      if (!pressed) return idle() // insertText/composition/paste has no remote key-down to release.
      return { events: [{ kind: 'key', type, ...pressed, modifiers: browserKeyModifiers(event) }], preventDefault: true }
    }
    if (this.composing || event.isComposing || event.keyCode === 229 || event.altGraph
      || modifierKeys.has(event.key) || ['Process', 'Dead', 'Unidentified'].includes(event.key)) return idle()
    this.discardedComposition = false
    this.committedText = null
    const paste = ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'v')
      || (event.shiftKey && event.key === 'Insert' && !event.ctrlKey && !event.metaKey && !event.altKey)
    if (paste) return idle() // Let the browser produce the user-initiated paste event; no clipboard API.
    if (Array.from(event.key).length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) return idle()
    if (!event.key || event.key.length > 32 || (event.code?.length ?? 0) > 32 || (this.pressed.size >= 8 && !this.pressed.has(id))) return { events: [], preventDefault: true }
    const value = { key: event.key, ...(event.code ? { code: event.code } : {}) }
    this.pressed.set(id, value)
    return { events: [{ kind: 'key', type, ...value, modifiers: browserKeyModifiers(event) }], preventDefault: true }
  }

  blur(scope: string | null): InputResult {
    const events = this.accepts(scope) ? [...this.pressed.values()].map(key => ({ kind: 'key', type: 'keyUp', ...key, modifiers: 0 })) : []
    this.pressed.clear()
    this.discardedComposition = this.discardedComposition || this.composing
    this.composing = false
    this.committedText = null
    return { events, preventDefault: false }
  }

  private commit(text: string): InputResult {
    // Python's [:512] counts Unicode code points. Refuse a whole oversized edit;
    // never silently truncate or split it into partly delivered messages.
    if (text.length > 1024) return { events: [], preventDefault: true, error: 'text_too_long' }
    let size = 0
    for (const char of text) {
      const code = char.codePointAt(0)!
      if (code === 0 || (code >= 0xd800 && code <= 0xdfff)) return { events: [], preventDefault: true, error: 'invalid_text' }
      if (++size > 512) return { events: [], preventDefault: true, error: 'text_too_long' }
    }
    return { events: text ? [{ kind: 'text', text }] : [], preventDefault: false }
  }
}
