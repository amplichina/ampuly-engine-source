import type { RelationGraphData, RelationPulse } from '../relations'
import type { ThemeModel } from '../themeModel'

export interface ThemeViewProps {
  data: RelationGraphData
  model: ThemeModel
  selectedId: string
  reducedMotion: boolean
  pulses: RelationPulse[]
  onSelect: (id: string) => void
  onExpandFold: (managerId: string) => void
  /** Called when the renderer cannot start (e.g. WebGL context failure). */
  onUnsupported?: () => void
}

export function memberName(model: ThemeModel, id: string): string {
  return model.members.find(node => node.id === id)?.label ?? (model.owner?.id === id ? model.owner.label : id)
}

export function clockText(time: number): string {
  const ms = time < 1e11 ? time * 1000 : time
  const date = new Date(ms)
  if (!Number.isFinite(date.getTime())) return '--:--'
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`
}
