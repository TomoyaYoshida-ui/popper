import { useRef, useState } from 'react'
import type { SessionIndex, SessionSummary } from '../sessionNavigation'
import { filterProjects, recentSessions } from '../sessionNavigation'
import { WorkbenchIcon } from './WorkbenchIcon'

export function ProjectSessionTree({ index, currentProject, currentSession, loading, error, busy, onRetry, onOpen, onCreate, onChange }: {
  index: SessionIndex; currentProject: string; currentSession: string | null; loading: boolean; error: string | null; busy: boolean
  onRetry: () => void; onOpen: (project: string, session?: string) => void; onCreate: (project: string) => void
  onChange?: (session: SessionSummary, change: { title: string } | { status: 'active' | 'archived' }) => Promise<void>
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [query, setQuery] = useState('')
  const [editing, setEditing] = useState<SessionSummary | null>(null)
  const [title, setTitle] = useState('')
  const [actionError, setActionError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const locked = useRef(false)
  const projectLabel = (project: SessionIndex['projects'][number]) => index.projects.filter((item) => item.name === project.name).length > 1
    ? `${project.name} · ${project.root?.split(/[\\/]/).filter(Boolean).pop() ?? project.workspace_id.slice(-6)}` : project.name
  const change = async (session: SessionSummary, update: { title: string } | { status: 'active' | 'archived' }) => {
    if (!onChange || locked.current || busy) return
    locked.current = true; setSaving(true); setActionError(null)
    try { await onChange(session, update); setEditing(null) }
    catch (reason) { setActionError((reason as Error).message) }
    finally { locked.current = false; setSaving(false) }
  }
  const row = (session: SessionSummary, recent = false) => <div className={`project-session-row ${currentProject === session.workspace_id && currentSession === session.session_id ? 'selected' : ''}`} key={`${recent ? 'recent' : 'tree'}:${session.workspace_id}:${session.session_id}`}>
    <button type="button" className="project-session-link" disabled={busy || session.status === 'archived'}
      onClick={() => onOpen(session.workspace_id, session.session_id)} title={session.status === 'archived' ? `${session.title} · 已归档，恢复后可打开` : session.title} aria-current={currentProject === session.workspace_id && currentSession === session.session_id ? 'page' : undefined}>
      <span className="session-title">{session.title}</span>
      {session.running ? <span className="session-state running" title="正在运行">运行中</span> : session.pending_approvals > 0 ? <span className="session-state approval">待审批</span> : null}
      {recent && <small>{index.projects.find((project) => project.workspace_id === session.workspace_id) && projectLabel(index.projects.find((project) => project.workspace_id === session.workspace_id)!)}</small>}
    </button>
    {onChange && <details className="session-row-menu"><summary aria-label={`${session.title} 的操作`} title="会话操作"><WorkbenchIcon kind="more" /></summary><div className="session-menu-content">
      <button type="button" disabled={busy || saving} onClick={(event) => { setEditing(session); setTitle(session.title); setActionError(null); event.currentTarget.closest('details')?.removeAttribute('open') }}>重命名</button>
      <button type="button" disabled={busy || saving || session.running} title={session.running ? '请先停止运行，再归档会话' : undefined}
        onClick={() => { void change(session, { status: session.status === 'archived' ? 'active' : 'archived' }) }}>{session.status === 'archived' ? '恢复会话' : '归档会话'}</button>
    </div></details>}
  </div>
  const projects = filterProjects(index, query)
  return <div className="project-navigation">
    <label className="session-search"><svg className="i" viewBox="0 0 24 24"><circle cx="10" cy="10" r="6" /><path d="m15 15 5 5" /></svg><input aria-label="搜索项目与会话" placeholder="搜索项目或会话…" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
    <div className="sidebar-section-label">项目</div>
    {loading && <p className="sidebar-empty" role="status">正在读取项目与会话…</p>}
    {error && <div className="sidebar-read-error" role="alert">{error}<button type="button" onClick={onRetry}>重新读取</button></div>}
    {!loading && !error && !projects.length && <p className="sidebar-empty">没有匹配的项目或会话</p>}
    {projects.map((project) => {
      const open = !!query.trim() || (expanded[project.workspace_id] ?? project.workspace_id === currentProject)
      const active = project.sessions.filter((session) => session.status === 'active')
      const archived = project.sessions.filter((session) => session.status === 'archived')
      const label = projectLabel(project)
      const disambiguated = label !== project.name
      return <section className="project-tree-group" role="group" aria-label={`${label} 的会话`} key={project.workspace_id}>
        <div className={`project-tree-heading ${project.workspace_id === currentProject ? 'current' : ''}`}>
          <button type="button" className="project-disclosure" aria-label={`${open ? '收起' : '展开'} ${label} 的会话`} aria-expanded={open} onClick={() => setExpanded((value) => ({ ...value, [project.workspace_id]: !open }))}
            onKeyDown={(event) => { if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') { event.preventDefault(); setExpanded((value) => ({ ...value, [project.workspace_id]: event.key === 'ArrowRight' })) } }}><WorkbenchIcon kind="chevron" /></button>
          <button type="button" className="project-tree-name" aria-label={label} disabled={busy} onClick={() => onOpen(project.workspace_id)} title={project.root ? `${project.name}\n${project.root}` : project.name}><svg className="i" viewBox="0 0 24 24"><path d="M3 6h7l2 2h9v12H3z" /></svg><span className="project-name-lines"><span>{project.name}</span>{disambiguated && <small>{project.root?.split(/[\\/]/).filter(Boolean).pop() ?? project.workspace_id.slice(-6)}</small>}</span></button>
          <button type="button" className="project-create" aria-label={`在 ${label} 新建会话`} title="在此项目新建会话" disabled={busy} onClick={() => onCreate(project.workspace_id)}><WorkbenchIcon kind="plus" /></button>
        </div>
        {open && <div className="project-tree-sessions">
          {project.error ? <div className="sidebar-read-error">{project.error}<button type="button" onClick={onRetry}>重试</button></div>
            : loading ? null : <>{active.map((session) => row(session))}{!active.length && <p className="sidebar-empty">{query ? '没有匹配的会话' : '尚无会话'}</p>}
              {!!archived.length && <details className="archived-sessions" open={query.trim() ? true : undefined}><summary>已归档 ({archived.length})</summary>{archived.map((session) => row(session))}</details>}</>}
        </div>}
      </section>
    })}
    {!query && <details className="recent-sessions"><summary>最近会话</summary>{recentSessions(index).map((session) => row(session, true))}{!loading && !recentSessions(index).length && <p className="sidebar-empty">暂无最近会话</p>}</details>}
    {actionError && <div role="alert" className="sidebar-read-error">{actionError}
      {editing && <button type="button" disabled={saving} onClick={() => {
        onRetry()
        const latest = index.projects.flatMap((project) => project.sessions).find((session) => session.workspace_id === editing.workspace_id && session.session_id === editing.session_id)
        if (latest) setEditing(latest)
      }}>核对最新版本</button>}
    </div>}
    {editing && <form className="session-rename" onSubmit={(event) => { event.preventDefault(); void change(editing, { title: title.trim() }) }}>
      <label>重命名会话<input aria-label="会话名称" autoFocus maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
      <div><button type="button" disabled={saving} onClick={() => setEditing(null)}>取消</button><button type="submit" disabled={saving || busy || !title.trim()}>{saving ? '保存中…' : '保存名称'}</button></div>
    </form>}
  </div>
}
