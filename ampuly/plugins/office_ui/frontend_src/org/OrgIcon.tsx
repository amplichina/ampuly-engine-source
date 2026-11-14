// The organisation pages' icons, as the shell draws its own (components/shell/ShellIcons): inline strokes in
// currentColor, so an icon takes its colour from where it sits -- the accent on the active tab, the theme's text
// colour elsewhere, light on the midnight palette. They used to be filled data-URIs with #888 baked in, shown through
// <img>; CSS could only fade those, never tint them. No icon library is installed; these are hand-written paths.

const PATHS = {
  team: ['M8.5 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M2.5 19.5c.4-3.3 2.8-5 6-5s5.6 1.7 6 5', 'M16 11a2.6 2.6 0 1 0-1-5', 'M17.5 14.6c2.3.5 3.7 2.1 4 4.9'],
  runtime: ['M20 12a8 8 0 1 1-2.6-5.9', 'M20 4v4.5h-4.5', 'M12 8v4.2l2.8 1.8'],
  architecture: ['M9.5 3.5h5v4h-5z', 'M3.5 16.5h5v4h-5z', 'M15.5 16.5h5v4h-5z', 'M12 7.5v4.5', 'M6 16.5V12h12v4.5'],
  agents: ['M6 8.5h12a1.5 1.5 0 0 1 1.5 1.5v8A1.5 1.5 0 0 1 18 19.5H6A1.5 1.5 0 0 1 4.5 18v-8A1.5 1.5 0 0 1 6 8.5z', 'M12 8.5V5', 'M12 5a1 1 0 1 0 0-2 1 1 0 0 0 0 2z', 'M9 13h.01', 'M15 13h.01', 'M9.5 16.2h5'],
  person: ['M12 11.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z', 'M5 20c.5-3.6 3.2-5.5 7-5.5s6.5 1.9 7 5.5'],
  active: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M10 8.5l5.5 3.5-5.5 3.5z'],
  rocket: ['M12 3c3.4 2.2 5 5.3 5 9l-2 3.5H9L7 12c0-3.7 1.6-6.8 5-9z', 'M12 10.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z', 'M9 15.5l-2.5 4 3.5-1', 'M15 15.5l2.5 4-3.5-1'],
  check: ['M5 12.5l4.5 4.5L19 7.5'],
  search: ['M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13z', 'M15.5 15.5L20 20'],
  package: ['M12 3l8 4.2v9.6L12 21l-8-4.2V7.2z', 'M4 7.2l8 4.3 8-4.3', 'M12 11.5V21'],
  channels: ['M4 5.5h16v10H9.5L5.5 19v-3.5H4z', 'M8 9.5h8', 'M8 12.5h5'],
  reorg: ['M7 4v12', 'M4 13l3 3 3-3', 'M17 20V8', 'M14 11l3-3 3 3'],
  download: ['M12 4v11', 'M7.5 11l4.5 4.5 4.5-4.5', 'M5 19.5h14'],
  upload: ['M12 15.5V4.5', 'M7.5 9l4.5-4.5L16.5 9', 'M5 19.5h14'],
  arrow: ['M4.5 12h15', 'M14 6.5l5.5 5.5-5.5 5.5'],
  gate: ['M12 3l7.5 3v5.5c0 4.5-3 8-7.5 9.5-4.5-1.5-7.5-5-7.5-9.5V6z', 'M9 12l2.2 2.2L15.2 10'],
  hold: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M9.5 9v6', 'M14.5 9v6'],
  review: ['M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z', 'M12 14.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z'],
  warn: ['M12 4l9 15.5H3z', 'M12 10v4.5', 'M12 17.3h.01'],
  templates: ['M5 4.5h14v15H5z', 'M8.5 9h7', 'M8.5 12.5h7', 'M8.5 16h4'],
  addPerson: ['M10 11.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z', 'M3 20c.5-3.6 3.2-5.5 7-5.5 1.6 0 3 .3 4.2 1', 'M18.5 14v6', 'M15.5 17h6'],
  trash: ['M4.5 7h15', 'M9.5 7V4.5h5V7', 'M6.5 7l.8 12.5h9.4l.8-12.5', 'M10 11v5', 'M14 11v5'],
} as const

export type OrgIconName = keyof typeof PATHS

export function OrgIcon({ name, size = 16, className }: { name: OrgIconName; size?: number; className?: string }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {PATHS[name].map(d => <path key={d} d={d} />)}
    </svg>
  )
}
