import type { ComponentProps } from 'react'
import { AIEmployeeEditor } from '../AIEmployeeEditor'
import { AIEmployeeLibrary } from './AIEmployeeLibrary'
import { AIEmployeeShelf } from './AIEmployeeShelf'
import { AIEmployeeNodeBinding } from './AIEmployeeNodeBinding'
import { AIEmployeeProjectPanel } from './AIEmployeeProjectPanel'
import { AIEmployeeRunPanel } from './AIEmployeeRunPanel'
import { AIEmployeeReworkCard } from './AIEmployeeReworkCard'
import { AIEmployeeProjectionBoundary } from './AIEmployeeProjectionBoundary'
import type { AIEmployeeProjection, AIEmployeeProjectionBoundaryProps } from './AIEmployeeProjectionBoundary'

/** Slots in existing pages, not a second navigation, store, agent loop or wire contract. */
export type AIEmployeeEntryPoint =
  | { kind: 'library'; props: ComponentProps<typeof AIEmployeeLibrary> }
  | { kind: 'shelf'; props: ComponentProps<typeof AIEmployeeShelf> }
  | { kind: 'node'; props: ComponentProps<typeof AIEmployeeNodeBinding> }
  | { kind: 'project'; props: ComponentProps<typeof AIEmployeeProjectPanel> }
  | { kind: 'run'; props: ComponentProps<typeof AIEmployeeRunPanel> }
  | { kind: 'rework'; props: ComponentProps<typeof AIEmployeeReworkCard> }
  | { kind: 'editor'; props: ComponentProps<typeof AIEmployeeEditor> }

export interface AIEmployeeEntryPointsProps {
  entry: AIEmployeeEntryPoint
  boundary: Omit<AIEmployeeProjectionBoundaryProps, 'children'>
}

/** Cross-check the leaf's actual scope, not only the boundary's own requested/observed pair. */
export function aiEmployeeEntryScopeReason(entry: AIEmployeeEntryPoint, projection: AIEmployeeProjection): string {
  let actualProject: string | null
  switch (entry.kind) {
    case 'library': actualProject = entry.props.project?.project_id ?? null; break
    case 'shelf': actualProject = entry.props.project_id; break
    case 'node': actualProject = entry.props.project_id; break
    case 'project': actualProject = entry.props.project_id; break
    case 'run':
      actualProject = entry.props.project_id
      if (entry.props.run && entry.props.run.project_id !== actualProject) return '运行内容与入口项目不一致，不展示或操作。'
      break
    case 'rework':
      actualProject = entry.props.project_id
      if (entry.props.record && entry.props.record.project_id !== actualProject) return '返修内容与入口项目不一致，不展示或操作。'
      break
    case 'editor':
      if (entry.props.scope === 'standard_package') {
        // A global package scope is explicit. Incidental project/instance/node fields do not turn it into a project edit.
        actualProject = null
      } else if (entry.props.scope === 'project_instance' || entry.props.scope === 'node_override') {
        actualProject = entry.props.target.project_id ?? null
        if (!actualProject) return '项目或节点编辑缺少明确所属项目，不能退成全局标准包编辑。'
      } else return '编辑作用范围尚未明确；先由宿主选择项目实例、本节点或全局标准包。'
      break
  }
  if (actualProject !== null && (!actualProject.trim() || actualProject !== actualProject.trim())) return '入口项目标识尚未明确，不展示或套用其他项目。'
  if (entry.kind !== 'library' && !(entry.kind === 'editor' && entry.props.scope === 'standard_package') && actualProject === null) return '此入口需要明确项目；全局目录不能代替项目范围。'
  if (actualProject !== projection.requested_project_id || actualProject !== projection.observed_project_id) {
    return '入口内容所属项目与当前读取范围不一致，不展示或套用；全局标准包需使用明确的全局读取范围。'
  }
  return ''
}

export function AIEmployeeEntryPoints({ entry, boundary }: AIEmployeeEntryPointsProps) {
  const scopeReason = aiEmployeeEntryScopeReason(entry, boundary.projection)
  if (scopeReason) return <AIEmployeeProjectionBoundary {...boundary} projection={{ ...boundary.projection, state: 'stale', message: scopeReason }}>{null}</AIEmployeeProjectionBoundary>
  let content
  switch (entry.kind) {
    case 'library': content = <AIEmployeeLibrary {...entry.props} />; break
    case 'shelf': content = <AIEmployeeShelf {...entry.props} />; break
    case 'node': content = <AIEmployeeNodeBinding {...entry.props} />; break
    case 'project': content = <AIEmployeeProjectPanel {...entry.props} />; break
    case 'run': content = <AIEmployeeRunPanel {...entry.props} />; break
    case 'rework': content = <AIEmployeeReworkCard {...entry.props} />; break
    case 'editor': content = <AIEmployeeEditor {...entry.props} />; break
  }
  return <AIEmployeeProjectionBoundary {...boundary}>{content}</AIEmployeeProjectionBoundary>
}
