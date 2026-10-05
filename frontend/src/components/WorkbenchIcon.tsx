/** One stroke language for navigation, documents and actions. Icons never supply accessible names. */
const paths = {
  chat: 'M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 3V6a2 2 0 0 1 2-2Z',
  study: 'M7 3h10a2 2 0 0 1 2 2v16H5V5a2 2 0 0 1 2-2Z M9 7h6 M9 11h6 M9 15h4',
  files: 'M3 7V5h7l2 3h8a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Z',
  runs: 'm8 4 12 8-12 8V4Z',
  sidebar: 'M3 4h18v16H3Z M9 4v16',
  expand: 'M8 3H3v5 M16 3h5v5 M21 16v5h-5 M3 16v5h5',
  terminal: 'm4 7 5 5-5 5 M12 17h8',
  plus: 'M12 5v14 M5 12h14',
  close: 'm6 6 12 12 M6 18 18 6',
  more: 'M5 12h.01 M12 12h.01 M19 12h.01',
  chevron: 'm9 5 7 7-7 7',
  arrowUp: 'M12 19V5 m-6 6 6-6 6 6',
  arrowOut: 'M7 17 17 7 M7 7h10v10',
  stop: 'M7 7h10v10H7Z',
  settings: 'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1 1-3Z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  file: 'M14 3H5v18h14V8l-5-5Z M14 3v5h5 M8 13h8 M8 17h5',
  code: 'm8 8-4 4 4 4 m8-8 4 4-4 4 M14 5l-4 14',
  data: 'M4 5h16v14H4Z M4 10h16 M10 5v14 M4 14h16',
  markdown: 'M3 6h18v12H3Z M6 15V9l3 3 3-3v6 M17 9v6 m-2-2 2 2 2-2',
  lock: 'M7 11V7a5 5 0 0 1 10 0v4 M5 11h14v10H5Z M12 15v2',
  refresh: 'M20 7v5h-5 M20 12a8 8 0 1 0-2 6',
  search: 'M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0Z m-1 5 5 5',
  orbit: 'M19 5c3 3 1 8-3 12s-9 6-12 3-1-8 3-12 9-6 12-3Z M5 5c3-3 8-1 12 3s6 9 3 12-8 1-12-3-6-9-3-12Z M12 11v2',
} as const
export type WorkbenchIconKind = keyof typeof paths
export function WorkbenchIcon({ kind, className = '' }: { kind: WorkbenchIconKind; className?: string }) {
  return <svg viewBox="0 0 24 24" className={`i ${className}`} aria-hidden="true" focusable="false"><path d={paths[kind]} /></svg>
}
