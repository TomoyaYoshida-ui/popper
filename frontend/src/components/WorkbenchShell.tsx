import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { ChatState, Workspace } from '../types'
import type { ResearchState } from '../api'
import { defaultLayout, openStudyResource, removeTab, restoreProjectLayout, projectLayoutKey, type EditorController, type WorkbenchLayout, type WorkbenchResource, type WorkbenchTab } from '../workbench'
import type { SessionIndex, SessionSummary } from '../sessionNavigation'
import { ProjectSessionTree } from './ProjectSessionTree'
import { SettingsMenu } from './SettingsMenu'
import { ProviderSettings } from './ProviderSettings'
import { PanelBoundary } from './PanelBoundary'
import { FileTree } from './FileTree'
import { WorkbenchIcon as Icon } from './WorkbenchIcon'

const TerminalView = lazy(() => import('./TerminalView').then((module) => ({ default: module.TerminalView })))
type PanelContext = { openResource: (resource: WorkbenchResource) => void; selectedStudy: string | null; selectStudy: (run: string) => void; registerEditor: (controller: EditorController) => void; protectProjectChange: (action: () => void) => void }
const QUICK_LINKS: WorkbenchResource[] = [
  { kind: 'protocol', title: '项目与协议' }, { kind: 'evidence', title: '文献与证据' }, { kind: 'manuscripts', title: '研究稿件' },
]
const ADVANCED_LINKS: WorkbenchResource[] = [
  { kind: 'approvals', title: '项目评审裁决' }, { kind: 'review', title: '项目评审报告' },
  { kind: 'environment', title: '实验环境' }, { kind: 'progress', title: '研究进度' }, { kind: 'quests', title: '历史任务' },
]
export function WorkbenchShell({ workspace, research, chat, error, connected, terminalAvailable, filesAvailable = false, sessionIndex, indexLoading = false, indexError = null, transition = null, onIndexRetry, onSessionChange, onRetry, onDismissError, onNewSession, onSelectSession, onSwitchProject, onMaterialize, renderChat, renderTab, renderLogs, renderAudit }: {
  workspace: Workspace | null; research: ResearchState | null; chat: ChatState | null; error: string | null; connected: boolean; terminalAvailable: boolean
  filesAvailable?: boolean
  sessionIndex?: SessionIndex; indexLoading?: boolean; indexError?: string | null; transition?: string | null
  onIndexRetry?: () => void; onSessionChange?: (session: SessionSummary, change: { title: string } | { status: 'active' | 'archived' }) => Promise<void>
  onRetry: () => void; onDismissError: () => void; onNewSession: (projectId?: string) => Promise<void>; onSelectSession: (id: string, projectId?: string) => Promise<void>
  onSwitchProject: (id: string) => Promise<void>; onMaterialize: () => void
  renderChat: (contextKey: string, openResource: (resource: WorkbenchResource) => void, studyLabel: string | null) => ReactNode
  renderTab: (tab: WorkbenchTab, context: PanelContext) => ReactNode
  renderLogs: (openResource: (resource: WorkbenchResource) => void) => ReactNode; renderAudit: () => ReactNode
}) {
  const projectId = workspace?.current_root ?? 'connecting'
  const contextKey = `${projectId}:${chat?.session_id ?? 'new'}`
  const layoutKey = projectLayoutKey(projectId)
  const readLayout = () => { try { return restoreProjectLayout(localStorage, projectId, chat?.session_id ?? null, chat?.sessions ?? [], window.innerWidth) } catch { return defaultLayout(window.innerWidth) } }
  const [savedState, setSavedState] = useState(() => {
    return { key: layoutKey, layout: readLayout() }
  })
  const layout = savedState.key === layoutKey ? savedState.layout : defaultLayout(window.innerWidth)
  const update = useCallback((change: (layout: WorkbenchLayout) => WorkbenchLayout) => {
    setSavedState((current) => ({ key: layoutKey, layout: change(current.key === layoutKey ? current.layout : defaultLayout(window.innerWidth)) }))
  }, [layoutKey])
  const [notice, setNotice] = useState<string | null>(null)
  const [dirtyIds, setDirtyIds] = useState<string[]>([])
  const editors = useRef(new Map<string, EditorController>())
  const [pendingClose, setPendingClose] = useState<{ ids: string[]; action: () => void } | null>(null)
  const [saving, setSaving] = useState(false)
  const [terminalMounted, setTerminalMounted] = useState(false)
  const [terminalGeneration, setTerminalGeneration] = useState(0)
  const workspaceRef = useRef<HTMLDivElement>(null)
  const dialogRef = useRef<HTMLElement>(null)
  const newSessionRef = useRef(onNewSession)
  newSessionRef.current = onNewSession
  const project = workspace?.roots.find((root) => root.wid === projectId)
  useEffect(() => { workspaceRef.current?.querySelector('.workbench-columns')?.toggleAttribute('inert', !!transition) }, [transition])
  const selectedRun = research?.runs.find((run) => run.run_dir === layout.selectedStudy) ?? research?.runs.filter((run) => run.ok).slice(-1)[0]
  const active = layout.tabs.find((tab) => tab.id === layout.activeTab)
  const fileContext = active?.kind === 'files' || active?.kind === 'file'
  const hasFileTabs = layout.tabs.some((tab) => tab.kind === 'files' || tab.kind === 'file')
  useEffect(() => {
    const reveal = () => document.getElementById(`tab-${encodeURIComponent(layout.activeTab ?? '')}`)?.scrollIntoView?.({ block: 'nearest', inline: 'nearest', behavior: 'auto' })
    reveal()
    window.addEventListener('resize', reveal)
    return () => window.removeEventListener('resize', reveal)
  }, [layout.activeTab, layout.sidebarOpen, layout.sidebarWidth, layout.chatWidth, layout.maximized])

  useEffect(() => {
    if (!pendingClose) return
    const previousFocus = document.activeElement as HTMLElement | null
    const dialog = dialogRef.current
    const trap = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || !dialog) return
      const buttons = [...dialog.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
      if (!buttons.length) { event.preventDefault(); return }
      const first = buttons[0], last = buttons[buttons.length - 1]
      if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', trap)
    return () => { document.removeEventListener('keydown', trap); if (previousFocus?.isConnected) previousFocus.focus() }
  }, [pendingClose])

  useEffect(() => {
    setSavedState({ key: layoutKey, layout: readLayout() })
    editors.current.clear(); setDirtyIds([]); setPendingClose(null); setNotice(null)
  // Session selection never resets project-owned editors or layout.
  }, [layoutKey, !!chat])
  useEffect(() => {
    if (savedState.key !== layoutKey || !workspace || !chat) return
    try { localStorage.setItem(layoutKey, JSON.stringify(savedState.layout)) } catch { /* Layout is still usable without storage. */ }
  }, [savedState, layoutKey, !!chat, !!workspace])
  useEffect(() => { setTerminalMounted(false); setTerminalGeneration((value) => value + 1) }, [projectId])
  useEffect(() => {
    if (layout.bottomOpen && layout.bottomTab === 'terminal' && terminalAvailable) setTerminalMounted(true)
  }, [layout.bottomOpen, layout.bottomTab, terminalAvailable])
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (![...editors.current.values()].some((editor) => editor.dirty)) return
      event.preventDefault(); event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [])
  const openResource = useCallback((resource: WorkbenchResource) => update((current) => ({ ...openStudyResource(current, resource, selectedRun?.manifest?.study_id),
    ...(resource.kind === 'files' ? { fileExplorerOpen: true } : {}) })), [update, selectedRun?.manifest?.study_id])
  const showBottom = useCallback((tab: WorkbenchLayout['bottomTab']) => update((current) => ({ ...current, bottomOpen: true, bottomTab: tab })), [update])
  useEffect(() => {
    const keys = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement
      if (event.key === '`' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); update((current) => ({ ...current, bottomOpen: !current.bottomOpen, bottomTab: 'terminal' })); return }
      if (event.key.toLowerCase() === 'b' && (event.ctrlKey || event.metaKey) && !target.closest('textarea,input,.monaco-editor,[contenteditable]')) { event.preventDefault(); update((current) => ({ ...current, sidebarOpen: !current.sidebarOpen })) }
      if (event.altKey && event.key.toLowerCase() === 'n') { event.preventDefault(); void newSessionRef.current() }
      if (event.key === 'Escape') { setPendingClose(null); update((current) => ({ ...current, maximized: false, sidebarOpen: window.innerWidth <= 1100 ? false : current.sidebarOpen })) }
    }
    window.addEventListener('keydown', keys)
    return () => window.removeEventListener('keydown', keys)
  }, [update])
  const registerEditor = (id: string, controller: EditorController) => {
    const wasDirty = editors.current.get(id)?.dirty ?? false
    editors.current.set(id, controller)
    if (wasDirty !== controller.dirty) setDirtyIds([...editors.current].filter(([, item]) => item.dirty).map(([key]) => key))
  }
  const requestClose = (ids: string[], action: () => void) => {
    if (transition) return
    const dirty = ids.filter((id) => editors.current.get(id)?.dirty)
    if (dirty.length) setPendingClose({ ids: dirty, action })
    else action()
  }
  const closeTab = (id: string) => requestClose([id], () => { editors.current.delete(id); setDirtyIds((items) => items.filter((item) => item !== id)); update((current) => removeTab(current, id)) })
  const navigate = (target: string, action: () => void) => {
    if (transition) return
    if (target === projectId) action()
    else requestClose(layout.tabs.map((tab) => tab.id), action)
  }
  const treeIndex = sessionIndex ? { projects: sessionIndex.projects.map((item) => ({ ...item, root: workspace?.roots.find((root) => root.wid === item.workspace_id)?.root })) }
    : { projects: workspace?.roots.map((root) => ({ workspace_id: root.wid, name: root.name, root: root.root, error: null,
    sessions: root.wid === projectId ? (chat?.sessions ?? []).map((session) => ({ ...session, workspace_id: root.wid, running: false, pending_approvals: 0 })) : [] })) ?? [] }
  const closeOthers = (id: string) => requestClose(layout.tabs.filter((tab) => tab.id !== id).map((tab) => tab.id), () => {
    for (const tab of layout.tabs) if (tab.id !== id) editors.current.delete(tab.id)
    setDirtyIds([...editors.current].filter(([, item]) => item.dirty).map(([key]) => key))
    update((current) => ({ ...current, tabs: current.tabs.filter((tab) => tab.id === id), activeTab: id }))
  })
  const resolveClose = async (mode: 'save' | 'discard') => {
    if (!pendingClose || saving) return
    setSaving(true)
    try {
      for (const id of pendingClose.ids) {
        const editor = editors.current.get(id)
        if (!editor?.dirty) continue
        if (mode === 'save' && !await editor.save()) {
          update((current) => ({ ...current, activeTab: id, mobileView: 'work' }))
          setNotice('请先在文件标签中核对差异并确认写回，再继续操作。'); setPendingClose(null); return
        }
        if (mode === 'discard') await editor.discard()
      }
      pendingClose.action(); setPendingClose(null)
    } catch (reason) { setNotice((reason as Error).message) }
    finally { setSaving(false) }
  }
  const resize = (event: React.PointerEvent<HTMLDivElement>, field: 'sidebarWidth' | 'chatWidth' | 'bottomHeight') => {
    event.preventDefault()
    const start = field === 'bottomHeight' ? event.clientY : event.clientX
    const initial = layout[field]
    const target = event.currentTarget
    target.setPointerCapture(event.pointerId)
    const move = (next: PointerEvent) => {
      const delta = field === 'bottomHeight' ? start - next.clientY : next.clientX - start
      const max = field === 'sidebarWidth' ? 340 : field === 'chatWidth' ? Math.min(720, (workspaceRef.current?.clientWidth ?? 1000) - 370) : Math.min(600, window.innerHeight - 250)
      update((current) => ({ ...current, [field]: Math.max(field === 'sidebarWidth' ? 200 : field === 'chatWidth' ? 320 : 150, Math.min(max, initial + delta)) }))
    }
    const done = () => { target.removeEventListener('pointermove', move); target.removeEventListener('pointerup', done); target.removeEventListener('pointercancel', done) }
    target.addEventListener('pointermove', move); target.addEventListener('pointerup', done); target.addEventListener('pointercancel', done)
  }
  const splitter = (field: 'sidebarWidth' | 'chatWidth' | 'bottomHeight', label: string) => <div className={`wb-splitter ${field === 'bottomHeight' ? 'horizontal' : ''}`}
    role="separator" aria-label={label} aria-orientation={field === 'bottomHeight' ? 'horizontal' : 'vertical'} aria-valuenow={layout[field]} tabIndex={0}
    onPointerDown={(event) => resize(event, field)} onKeyDown={(event) => {
      const direction = field === 'bottomHeight' ? (event.key === 'ArrowUp' ? 1 : event.key === 'ArrowDown' ? -1 : 0) : (event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0)
      if (!direction) return
      event.preventDefault(); update((current) => ({ ...current, [field]: Math.min(field === 'sidebarWidth' ? 340 : field === 'chatWidth' ? 720 : 600, Math.max(field === 'sidebarWidth' ? 200 : field === 'chatWidth' ? 320 : 150, current[field] + direction * 20)) }))
    }} />

  return <div className={`workbench ${layout.sidebarOpen ? 'sidebar-open' : ''} ${layout.maximized ? 'work-maximized' : ''} mobile-${layout.mobileView}`}>
    <ProviderSettings workspaceId={projectId} />
    <nav className="activity-rail" aria-label="工作台导航">
      <button className="rail-brand" type="button" title="Popper · 返回当前 Study" aria-label="返回当前 Study" onClick={() => openResource({ kind: 'study' })}>P</button>
      <button type="button" title="Agent 会话" aria-label="Agent 会话" onClick={() => { update((current) => ({ ...current, mobileView: 'chat', maximized: false })); requestAnimationFrame(() => workspaceRef.current?.querySelector<HTMLTextAreaElement>('.agent-composer textarea')?.focus()) }}><Icon kind="chat" /></button>
      <button type="button" title="Study 与研究" aria-label="Study 与研究" onClick={() => openResource({ kind: 'study' })}><Icon kind="study" /></button>
      <button type="button" title="项目文件" aria-label="项目文件" onClick={() => openResource({ kind: 'files' })}><Icon kind="files" /></button>
      <button type="button" title="实验运行" aria-label="实验运行" onClick={() => openResource({ kind: 'runs' })}><Icon kind="runs" /></button>
      <button type="button" title="交互终端 · Ctrl+`" aria-label="交互终端" disabled={!terminalAvailable} onClick={() => showBottom('terminal')}><Icon kind="terminal" /></button>
      <div className="rail-spacer" /><SettingsMenu placement="rail" /><span className="rail-avatar" title="单用户本地工作区">P</span>
    </nav>
    {layout.sidebarOpen && <>
      <button type="button" className="sidebar-backdrop" aria-label="收起会话列表" onClick={() => update((current) => ({ ...current, sidebarOpen: false }))} />
      <aside className="session-sidebar" style={{ width: layout.sidebarWidth }} aria-label="项目与会话">
        <header className="sidebar-heading"><strong>Popper</strong><button type="button" className="icon-button" title="收起侧栏" aria-label="收起侧栏" onClick={() => update((current) => ({ ...current, sidebarOpen: false }))}><Icon kind="sidebar" /></button></header>
        <button type="button" className="new-session" disabled={!workspace || !!transition} title={`在 ${project?.name ?? '当前项目'} 新建会话`} onClick={() => { void onNewSession(projectId) }}><Icon kind="plus" />新会话 <kbd>Alt N</kbd></button>
        <ProjectSessionTree index={treeIndex} currentProject={projectId} currentSession={chat?.session_id ?? null}
          loading={indexLoading || !workspace || !chat} error={indexError} busy={!!transition} onRetry={onIndexRetry ?? onRetry} onChange={onSessionChange}
          onOpen={(id, session) => navigate(id, () => { void (session ? onSelectSession(session, id) : onSwitchProject(id)) })}
          onCreate={(id) => navigate(id, () => { void onNewSession(id) })} />
        <footer className="sidebar-footer"><span className={`connection-dot ${connected ? '' : 'offline'}`} /><span>{connected ? '本地科研工作区' : '正在连接工作区'}</span><button type="button" title="产物导出" onClick={onMaterialize}>导出</button></footer>
      </aside>{splitter('sidebarWidth', '调整会话列表宽度')}
    </>}
    <div className="workbench-body" ref={workspaceRef}>
      <header className="workbench-topbar">
        {!layout.sidebarOpen && <button type="button" className="icon-button" title="展开会话列表" aria-label="展开会话列表" onClick={() => update((current) => ({ ...current, sidebarOpen: true }))}><Icon kind="sidebar" /></button>}
        <span className="topbar-project" title={project?.root}>{project?.name ?? '连接工作区…'}</span><span className="topbar-divider">/</span><span className="topbar-label">科研工作台</span>
        <div className="mobile-pane-switch" role="group" aria-label="切换主面板"><button type="button" className={layout.mobileView === 'chat' ? 'active' : ''} onClick={() => update((current) => ({ ...current, mobileView: 'chat' }))}>对话</button><button type="button" className={layout.mobileView === 'work' ? 'active' : ''} onClick={() => update((current) => ({ ...current, mobileView: 'work' }))}>工作区</button></div>
        <div className="topbar-actions"><details className="workspace-menu"><summary aria-label="打开工作区工具">打开面板<Icon kind="plus" /></summary><div className="workspace-menu-list">{QUICK_LINKS.map((resource) => <button type="button" key={resource.kind} onClick={(event) => { openResource(resource); event.currentTarget.closest('details.workspace-menu')?.removeAttribute('open') }}>{resource.title}</button>)}
          <details className="advanced-menu"><summary>高级工具</summary>{ADVANCED_LINKS.map((resource) => <button type="button" key={resource.kind} onClick={(event) => { openResource(resource); event.currentTarget.closest('details.workspace-menu')?.removeAttribute('open') }}>{resource.title}</button>)}</details>
        </div></details></div>
      </header>
      {error && <div className="workbench-notice error" role="alert"><span>{error}</span><button type="button" onClick={onRetry}>重试</button><button type="button" aria-label="关闭错误提示" onClick={onDismissError}>×</button></div>}
      {!connected && workspace && <div className="workbench-notice" role="status"><span>实时连接已断开，正在自动重连。已有草稿仍被保留。</span><button type="button" onClick={onRetry}>重新读取</button></div>}
      {notice && <div className="workbench-notice" role="status"><span>{notice}</span><button type="button" aria-label="关闭提示" onClick={() => setNotice(null)}>×</button></div>}
      {workspace?.environment_label && <div className="environment-banner" role="status">{workspace.environment_label}</div>}
      {transition && <div className="workbench-notice" role="status">正在打开 {transition}… 发送与写操作暂不可用。</div>}
      <div className="workbench-columns" aria-busy={!!transition}>
        <div className="chat-column" style={{ width: layout.chatWidth }}><PanelBoundary key={contextKey}>{renderChat(contextKey, openResource, selectedRun?.run_dir.split(/[\\/]/).pop() ?? null)}</PanelBoundary></div>
        {splitter('chatWidth', '调整对话宽度')}
        <section className="work-column" aria-label="研究与文件工作区">
          {!!research?.runs.length && <div className="workspace-study-picker"><label>当前研究 <select aria-label="当前 Study" disabled={!!transition} value={selectedRun?.run_dir ?? ''} onChange={(event) => {
            const run = research.runs.find((item) => item.run_dir === event.target.value)
            update((current) => ({ ...openStudyResource(current, { kind: 'study', title: run?.study?.question || 'Study', studyId: run?.manifest?.study_id }), selectedStudy: event.target.value }))
          }}>{research.runs.map((run) => <option key={run.run_dir} value={run.run_dir}>{run.study?.question || run.run_dir.split(/[\\/]/).pop()}</option>)}</select></label></div>}
          <div className="work-tabs-toolbar"><div className="work-tabs" role="tablist" aria-label="工作标签">
            {layout.tabs.map((tab) => <div className={`work-tab ${layout.activeTab === tab.id ? 'active' : ''}`} key={tab.id}>
              <button type="button" role="tab" id={`tab-${encodeURIComponent(tab.id)}`} aria-controls={`panel-${encodeURIComponent(tab.id)}`} aria-selected={layout.activeTab === tab.id} title={tab.title} tabIndex={layout.activeTab === tab.id ? 0 : -1}
                onKeyDown={(event) => {
                  const index = layout.tabs.findIndex((item) => item.id === tab.id)
                  const next = event.key === 'Home' ? 0 : event.key === 'End' ? layout.tabs.length - 1 : event.key === 'ArrowRight' ? (index + 1) % layout.tabs.length : event.key === 'ArrowLeft' ? (index - 1 + layout.tabs.length) % layout.tabs.length : -1
                  if (next < 0) return
                  event.preventDefault(); const id = layout.tabs[next].id
                  update((current) => ({ ...current, activeTab: id }))
                  document.getElementById(`tab-${encodeURIComponent(id)}`)?.focus()
                }}
                onClick={() => update((current) => ({ ...current, activeTab: tab.id }))}><Icon kind={tab.kind === 'file' || tab.kind === 'manuscript' ? 'file' : tab.kind === 'files' ? 'files' : tab.kind === 'runs' || tab.kind === 'job' ? 'runs' : 'study'} /><span>{tab.title}</span>{dirtyIds.includes(tab.id) && <i className="dirty-dot" aria-label="有未保存内容" />}</button>
              <button type="button" className="close-tab" title={`关闭 ${tab.title}`} aria-label={`关闭 ${tab.title}`} onClick={() => closeTab(tab.id)}><Icon kind="close" /></button>
            </div>)}
          </div><div className="work-tab-actions">{active && <details><summary title="标签操作" aria-label="标签操作"><Icon kind="more" /></summary><div className="workspace-menu-list"><button type="button" onClick={() => closeOthers(active.id)}>关闭其他标签</button><button type="button" onClick={() => requestClose(layout.tabs.map((tab) => tab.id), () => { editors.current.clear(); setDirtyIds([]); update((current) => ({ ...current, tabs: [], activeTab: null })) })}>关闭所有标签</button></div></details>}
            {fileContext && <button type="button" className={`icon-button file-explorer-toggle ${layout.fileExplorerOpen !== false ? 'active' : ''}`} title={layout.fileExplorerOpen !== false ? '收起文件目录' : '展开文件目录'} aria-label={layout.fileExplorerOpen !== false ? '收起文件目录' : '展开文件目录'} aria-expanded={layout.fileExplorerOpen !== false} onClick={() => update((current) => ({ ...current, fileExplorerOpen: current.fileExplorerOpen === false }))}><Icon kind="files" /></button>}
            <button type="button" className="icon-button" title={layout.maximized ? '恢复分栏' : '最大化工作区'} aria-label={layout.maximized ? '恢复分栏' : '最大化工作区'} onClick={() => update((current) => ({ ...current, maximized: !current.maximized }))}><Icon kind="expand" /></button></div></div>
          <div className="work-resource-area"><div className="work-tab-panels">
            {layout.tabs.map((tab) => <section className={`work-tab-panel kind-${tab.kind}`} key={`${projectId}:${tab.id}`} role="tabpanel" id={`panel-${encodeURIComponent(tab.id)}`} aria-labelledby={`tab-${encodeURIComponent(tab.id)}`} hidden={layout.activeTab !== tab.id}>
              <PanelBoundary>{renderTab(tab, { openResource, selectedStudy: layout.selectedStudy, selectStudy: (run) => update((current) => ({ ...current, selectedStudy: run })), registerEditor: (controller) => registerEditor(tab.id, controller), protectProjectChange: (action) => requestClose(layout.tabs.map((item) => item.id), action) })}</PanelBoundary>
            </section>)}
            {!layout.tabs.length && <div className="work-empty"><span className="empty-symbol"><Icon kind="study" /></span><h2>打开一个工作面板</h2><p>研究、文件和产物会在这里显示。</p><button type="button" onClick={() => openResource({ kind: 'study' })}>打开当前 Study →</button></div>}
          </div>
          {hasFileTabs && <aside className="workspace-file-explorer" aria-label="项目文件目录" hidden={!fileContext || layout.fileExplorerOpen === false}>
            <PanelBoundary key={projectId}><FileTree workspaceId={workspace?.current_root ?? null} trustedLocal={filesAvailable} activePath={active?.kind === 'file' ? active.ref : undefined} onOpenResource={openResource} /></PanelBoundary>
          </aside>}</div>
          {layout.bottomOpen && <>{splitter('bottomHeight', '调整底部面板高度')}<div className="bottom-panel" style={{ height: layout.bottomHeight }}>
            <header className="bottom-tabs" role="tablist" aria-label="底部工具">
              {(['terminal', 'logs', 'audit'] as const).map((key) => <button type="button" role="tab" aria-selected={layout.bottomTab === key} key={key} className={layout.bottomTab === key ? 'active' : ''} onClick={() => showBottom(key)}>{{ terminal: '交互终端', logs: '运行日志', audit: '事件审计' }[key]}</button>)}
              <div className="bottom-actions">{layout.bottomTab === 'terminal' && <button type="button" disabled={!terminalMounted} onClick={() => { setTerminalMounted(false); update((current) => ({ ...current, bottomOpen: false })) }}>结束终端</button>}<button type="button" title="收起面板" aria-label="收起底部面板" onClick={() => update((current) => ({ ...current, bottomOpen: false }))}>⌄</button></div>
            </header>
            <div className="bottom-content" hidden={layout.bottomTab !== 'logs'}>{renderLogs(openResource)}</div>
            <div className="bottom-content" hidden={layout.bottomTab !== 'audit'}>{renderAudit()}</div>
          </div></>}
          <div className="terminal-retainer" style={{ height: layout.bottomOpen ? Math.max(110, layout.bottomHeight - 35) : 0 }} hidden={!layout.bottomOpen || layout.bottomTab !== 'terminal'}>
            {terminalMounted ? <PanelBoundary key={`${projectId}:${terminalGeneration}`}><Suspense fallback={<div className="panel-loading">正在连接终端…</div>}><TerminalView /></Suspense></PanelBoundary>
              : <div className="panel-loading">{terminalAvailable ? '打开终端后将创建新的 Shell 会话。' : '当前环境未启用交互终端。'}</div>}
          </div>
          <footer className="work-statusbar"><span className={`connection-dot ${connected ? '' : 'offline'}`} />{project?.name ?? '本地工作区'}<span className="statusbar-spacer" />
            <button type="button" onClick={() => showBottom('logs')}>运行日志</button><button type="button" onClick={() => showBottom('audit')}>审计</button><button type="button" disabled={!terminalAvailable} onClick={() => showBottom('terminal')}>终端</button></footer>
        </section>
      </div>
    </div>
    {pendingClose && <div className="wb-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !saving) setPendingClose(null) }}><section ref={dialogRef} className="wb-dialog" role="dialog" aria-modal="true" aria-labelledby="unsaved-title">
      <h2 id="unsaved-title">有未保存的修改</h2><p>这些内容仍保存在当前编辑草稿中。</p><ul>{pendingClose.ids.map((id) => <li key={id}>{editors.current.get(id)?.label ?? id}</li>)}</ul>
      <div className="dialog-actions"><button type="button" disabled={saving} onClick={() => setPendingClose(null)} autoFocus>取消</button><button type="button" disabled={saving} onClick={() => { void resolveClose('discard') }}>放弃修改</button><button type="button" className="primary" disabled={saving} onClick={() => { void resolveClose('save') }}>{saving ? '处理中…' : '保存后继续'}</button></div>
    </section></div>}
  </div>
}
