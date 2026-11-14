import { Droppable } from '@hello-pangea/dnd'
import type { AgentInfo } from '../types/visual'
import type { KanbanColumn as KanbanColumnType, KanbanTask } from '../types/kanban'
import { KanbanCard } from './KanbanCard'
import { useI18n } from '../i18n'

interface KanbanColumnProps {
  column: KanbanColumnType
  tasks: KanbanTask[]
  agents: AgentInfo[]
  officeMap?: Record<string, string>
  companyMode?: boolean
  selectedTaskId?: string | null
  onCardClick: (task: KanbanTask) => void
}

// An order's steps, read only (policy, R1 option B): a column adds no card and starts none -- work starts
// from 业务工单 -- so it has no quick-add box and passes no start control to its cards.
export function KanbanColumn({ column, tasks, agents, officeMap, companyMode, selectedTaskId, onCardClick }: KanbanColumnProps) {
  const { translateMaybe } = useI18n()

  return (
    <div className="kanban-column">
      <div className="kanban-column-header">
        <span className="kanban-col-dot" style={{ background: column.color }} />
        <span className="kanban-col-label">{translateMaybe('kanban.column', column.name) || column.name}</span>
        <span className="kanban-col-count">{tasks.length}</span>
      </div>

      <Droppable droppableId={column.id} isDropDisabled={!!companyMode}>
        {(provided) => (
          <div ref={provided.innerRef} {...provided.droppableProps} className="kanban-col-body">
            {tasks.length === 0 && (
              <div className="kanban-empty"><span className="kanban-empty-icon">·</span></div>
            )}
            {tasks.map((task, index) => (
              <KanbanCard
                key={task.id}
                task={task}
                index={index}
                agents={agents}
                officeMap={officeMap}
                companyMode={companyMode}
                isSelected={task.id === selectedTaskId}
                onClick={onCardClick}
              />
            ))}
            {provided.placeholder}
          </div>
        )}
      </Droppable>
    </div>
  )
}
