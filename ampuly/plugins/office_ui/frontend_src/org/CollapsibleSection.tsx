import { useState, type ReactNode } from 'react'
import { OrgIcon, type OrgIconName } from './OrgIcon'

interface CollapsibleSectionProps {
  icon: OrgIconName
  title: string
  count: number
  extra?: ReactNode
  children: ReactNode
  defaultExpanded?: boolean
}

export function CollapsibleSection({ icon, title, count, extra, children, defaultExpanded = false }: CollapsibleSectionProps) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  return (
    <div className="myorg-collapsible">
      <button className="myorg-collapsible-toggle" onClick={() => setExpanded(!expanded)}>
        <span className="myorg-toggle-icon">{expanded ? '\u25BE' : '\u25B8'}</span>
        <OrgIcon name={icon} size={16} className="myorg-section-icon" />
        <span className="myorg-collapsible-title">{title}</span>
        <span className="myorg-collapsible-count">{count}</span>
        {extra && <span className="myorg-collapsible-extra" onClick={e => e.stopPropagation()}>{extra}</span>}
      </button>
      {expanded && <div className="myorg-collapsible-body">{children}</div>}
    </div>
  )
}
