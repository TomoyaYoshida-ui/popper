export type SessionSummary = {
  workspace_id: string; session_id: string; title: string; updated: string; created?: string
  status: 'active' | 'archived'; running: boolean; pending_approvals: number
}
export type SessionIndex = { projects: Array<{ workspace_id: string; name: string; root?: string; sessions: SessionSummary[]; error: string | null }> }
export function filterProjects(index: SessionIndex, query: string): SessionIndex['projects'] {
  const text = query.trim().toLocaleLowerCase()
  return index.projects.map((project) => ({ ...project, sessions: project.name.toLocaleLowerCase().includes(text)
    ? project.sessions : project.sessions.filter((session) => session.title.toLocaleLowerCase().includes(text)) }))
    .filter((project) => !text || project.name.toLocaleLowerCase().includes(text) || project.sessions.length > 0)
}
export function recentSessions(index: SessionIndex): SessionSummary[] {
  return index.projects.flatMap((project) => project.sessions).filter((session) => session.status === 'active')
    .sort((a, b) => b.updated.localeCompare(a.updated) || a.session_id.localeCompare(b.session_id)).slice(0, 10)
}
