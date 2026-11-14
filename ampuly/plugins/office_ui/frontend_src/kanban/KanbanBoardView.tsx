import { DragDropContext } from '@hello-pangea/dnd'
import type { AgentInfo } from '../types/visual'
import type { KanbanColumn as KanbanColumnType, KanbanTask } from '../types/kanban'
import { KanbanColumn } from './KanbanColumn'

interface KanbanBoardViewProps {
  columns: KanbanColumnType[]
  tasksByColumn: Record<string, KanbanTask[]>
  agents: AgentInfo[]
  officeMap?: Record<string, string>
  selectedTaskId?: string | null
  onCardClick: (task: KanbanTask) => void
}

// An order's steps, read only (policy, R1 option B): work starts from 业务工单, so nothing here adds a card,
// moves one between columns or runs one -- a card is only opened. The columns get their locked form (no drop, no
// drag, no add, no start), which is what ``companyMode`` means to KanbanColumn and KanbanCard; the drag-and-drop
// context stays only because the columns' drop areas need one, and it does nothing with a drop.
export function KanbanBoardView({
  columns, tasksByColumn, agents, officeMap, selectedTaskId, onCardClick,
}: KanbanBoardViewProps) {
  return (
    <DragDropContext onDragEnd={() => undefined}>
      <div className="kanban-board">
        {columns.map(col => (
          <KanbanColumn
            key={col.id}
            column={col}
            tasks={tasksByColumn[col.id] ?? []}
            agents={agents}
            officeMap={officeMap}
            companyMode
            selectedTaskId={selectedTaskId}
            onCardClick={onCardClick}
          />
        ))}
      </div>
    </DragDropContext>
  )
}
