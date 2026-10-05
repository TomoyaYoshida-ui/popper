export type ResourceKind = 'study' | 'protocol' | 'files' | 'file' | 'evidence' | 'claim' | 'observation' | 'job' | 'manuscript' | 'manuscripts' | 'runs' | 'progress' | 'review' | 'approvals' | 'environment' | 'overview' | 'diff' | 'quests'
export type WorkbenchResource = { kind: ResourceKind; title?: string; ref?: string; studyId?: string }
export type WorkbenchTab = WorkbenchResource & { id: string; title: string }
export type WorkbenchLayout = {
  version: 1; tabs: WorkbenchTab[]; activeTab: string | null; sidebarWidth: number; chatWidth: number
  bottomHeight: number; sidebarOpen: boolean; bottomOpen: boolean; bottomTab: 'terminal' | 'logs' | 'audit'
  selectedStudy: string | null; maximized: boolean; mobileView: 'chat' | 'work'
  fileExplorerOpen?: boolean
}
export type EditorDraft = { key: string; content: string; baseContent: string; baseSha256: string; updated: number; revisionMode?: boolean }
export type EditorController = { dirty: boolean; label: string; save: () => Promise<boolean>; discard: () => Promise<void> }

export const RESOURCE_LABELS: Record<ResourceKind, string> = {
  study: 'Study', protocol: '项目与协议', files: '打开文件', file: '文件', evidence: '文献与证据', claim: 'Claim',
  observation: 'Observation', job: '运行详情', manuscript: '稿件', manuscripts: '研究稿件', runs: '实验运行',
  progress: '研究进度', review: '项目评审报告', approvals: '项目评审裁决', environment: '实验环境', overview: 'Study',
  diff: '修改差异', quests: '历史任务',
}
const KINDS = Object.keys(RESOURCE_LABELS)
export function resourceTab(resource: WorkbenchResource): WorkbenchTab {
  if (resource.kind === 'overview') resource = { ...resource, kind: 'study', title: 'Study' }
  if (resource.kind === 'quests') resource = { ...resource, title: '历史任务' }
  if (resource.kind === 'files') resource = { ...resource, title: '打开文件' }
  return { ...resource, id: `${resource.kind}:${resource.studyId ?? ''}:${resource.ref ?? 'current'}`,
    title: resource.title || (resource.ref && ['file', 'manuscript'].includes(resource.kind) ? resource.ref.split(/[\\/]/).pop()! : RESOURCE_LABELS[resource.kind]) }
}
/** Bind the initial placeholder Study tab once its explicit identity is known. */
export function openStudyResource(layout: WorkbenchLayout, resource: WorkbenchResource, selectedStudyId?: string): WorkbenchLayout {
  if (resource.kind !== 'study') return openTab(layout, resource)
  const studyId = resource.studyId ?? selectedStudyId
  if (!studyId) return openTab(layout, resource)
  return openTab({ ...layout, tabs: layout.tabs.filter((tab) => tab.kind !== 'study' || !!tab.studyId) }, { ...resource, studyId })
}
export function defaultLayout(width = 1440): WorkbenchLayout {
  const tab = resourceTab({ kind: 'study' })
  return { version: 1, tabs: [tab], activeTab: tab.id, sidebarWidth: 248, chatWidth: 440, bottomHeight: 260,
    sidebarOpen: width > 1100, bottomOpen: false, bottomTab: 'terminal', selectedStudy: null, maximized: false, mobileView: 'chat' }
}
export function openTab(layout: WorkbenchLayout, resource: WorkbenchResource): WorkbenchLayout {
  const tab = resourceTab(resource)
  return { ...layout, tabs: layout.tabs.some((item) => item.id === tab.id) ? layout.tabs : [...layout.tabs, tab],
    activeTab: tab.id, mobileView: 'work' }
}
export function removeTab(layout: WorkbenchLayout, id: string): WorkbenchLayout {
  const index = layout.tabs.findIndex((tab) => tab.id === id)
  const tabs = layout.tabs.filter((tab) => tab.id !== id)
  return { ...layout, tabs, activeTab: layout.activeTab === id ? tabs[Math.min(index, tabs.length - 1)]?.id ?? null : layout.activeTab }
}
export function restoreLayout(raw: string | null, width: number): WorkbenchLayout {
  const fallback = defaultLayout(width)
  try {
    const saved = JSON.parse(raw ?? 'null') as Partial<WorkbenchLayout> | null
    if (!saved || saved.version !== 1 || !Array.isArray(saved.tabs)) return fallback
    const tabs: WorkbenchTab[] = []
    const migratedIds = new Map<string, string>()
    for (const item of saved.tabs.slice(0, 40)) {
      if (!item || !KINDS.includes(item.kind) || typeof item.title !== 'string' ||
        (item.ref != null && typeof item.ref !== 'string') || (item.studyId != null && typeof item.studyId !== 'string')) continue
      const tab = resourceTab(item)
      migratedIds.set(item.id, tab.id)
      if (!tabs.some((other) => other.id === tab.id)) tabs.push(tab)
    }
    const number = (value: unknown, min: number, max: number, base: number) => typeof value === 'number' && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : base
    const activeId = migratedIds.get(saved.activeTab ?? '') ?? saved.activeTab
    return { ...fallback, tabs, activeTab: tabs.some((tab) => tab.id === activeId) ? activeId! : tabs[0]?.id ?? null,
      sidebarWidth: number(saved.sidebarWidth, 200, 340, 248), chatWidth: number(saved.chatWidth, 320, 720, 440),
      bottomHeight: number(saved.bottomHeight, 150, 600, 260), sidebarOpen: typeof saved.sidebarOpen === 'boolean' ? saved.sidebarOpen : fallback.sidebarOpen,
      bottomTab: ['terminal', 'logs', 'audit'].includes(saved.bottomTab ?? '') ? saved.bottomTab! : 'terminal',
      bottomOpen: saved.bottomOpen === true, selectedStudy: typeof saved.selectedStudy === 'string' ? saved.selectedStudy : null,
      maximized: saved.maximized === true, mobileView: saved.mobileView === 'work' ? 'work' : 'chat',
      fileExplorerOpen: saved.fileExplorerOpen !== false }
  } catch { return fallback }
}

export const projectLayoutKey = (projectId: string) => `popper-workbench:v1:${projectId}:workspace`
/** Read-only migration: keep legacy keys and prioritize the selected session's preferences. */
export function restoreProjectLayout(storage: Pick<Storage, 'getItem'>, projectId: string, selectedSession: string | null,
  sessions: Array<{ session_id: string; updated: string }>, width: number): WorkbenchLayout {
  const saved = storage.getItem(projectLayoutKey(projectId))
  if (saved) return restoreLayout(saved, width)
  const ids = [...new Set([selectedSession ?? 'new', ...[...sessions].sort((a, b) => b.updated.localeCompare(a.updated)).map((session) => session.session_id), 'new'])]
  let result: WorkbenchLayout | null = null
  for (const id of ids) {
    const raw = storage.getItem(`popper-workbench:v1:${projectId}:${id}`)
    if (!raw) continue
    try { if (JSON.parse(raw).version !== 1 || !Array.isArray(JSON.parse(raw).tabs)) continue } catch { continue }
    const layout = restoreLayout(raw, width)
    if (!result) result = layout
    else for (const tab of layout.tabs) if (!result.tabs.some((other) => other.id === tab.id) && result.tabs.length < 40) result.tabs.push(tab)
  }
  return result ?? defaultLayout(width)
}

function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('popper-workbench', 1)
    request.onupgradeneeded = () => request.result.createObjectStore('drafts', { keyPath: 'key' })
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}
export async function readDraft(key: string): Promise<EditorDraft | null> {
  const db = await database()
  try { return await new Promise((resolve, reject) => {
    const transaction = db.transaction('drafts', 'readonly')
    const request = transaction.objectStore('drafts').get(key)
    request.onsuccess = () => resolve(request.result ?? null)
    request.onerror = () => reject(request.error)
  }) } finally { db.close() }
}
export async function writeDraft(draft: EditorDraft | string): Promise<void> {
  const db = await database()
  try { await new Promise<void>((resolve, reject) => {
    const transaction = db.transaction('drafts', 'readwrite')
    if (typeof draft === 'string') transaction.objectStore('drafts').delete(draft)
    else transaction.objectStore('drafts').put(draft)
    transaction.oncomplete = () => resolve()
    transaction.onerror = () => reject(transaction.error)
    transaction.onabort = () => reject(transaction.error)
  }) } finally { db.close() }
}
import type { ResearchState } from './api'

/** A previous project's response may arrive between workspace and research SSE patches. */
export function researchForProject(state: ResearchState | null, projectRoot?: string): ResearchState | null {
  if (!state || !projectRoot) return null
  const normalize = (path: string) => {
    const value = path.replace(/\\/g, '/').replace(/\/+$/, '')
    return /^[a-z]:\//i.test(value) ? value.toLowerCase() : value
  }
  return { ...state, runs: state.runs.filter((run) => !run.manifest?.project || normalize(run.manifest.project) === normalize(projectRoot)) }
}
