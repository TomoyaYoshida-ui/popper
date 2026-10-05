import { useEffect, useState } from 'react'
import { listProjectFiles, type ProjectFileEntry } from '../api'
import type { WorkbenchResource } from '../workbench'
import { WorkbenchIcon } from './WorkbenchIcon'

type Node = { name: string; path: string; children: Map<string, Node>; file?: ProjectFileEntry }
const sorted = (nodes: Node[]) => nodes.sort((a, b) => Number(!!a.file) - Number(!!b.file) || a.name.localeCompare(b.name))
export function fileTree(files: ProjectFileEntry[]): Node[] {
  const root: Node = { name: '', path: '', children: new Map() }
  for (const file of files) {
    let node = root
    const parts = file.path.replace(/\\/g, '/').split('/')
    parts.forEach((name, index) => {
      if (!node.children.has(name)) node.children.set(name, { name, path: parts.slice(0, index + 1).join('/'), children: new Map() })
      node = node.children.get(name)!
    })
    node.file = file
  }
  return sorted([...root.children.values()])
}
function FileIcon({ name }: { name: string }) {
  const extension = name.split('.').pop()?.toLowerCase()
  const kind = ['py', 'ts', 'tsx', 'js', 'jsx'].includes(extension ?? '') ? 'code' : ['json', 'csv', 'tsv'].includes(extension ?? '') ? 'data' : extension === 'md' ? 'markdown' : 'file'
  return <span className={`file-icon extension-${extension}`} aria-hidden="true"><WorkbenchIcon kind={kind} /></span>
}
function TreeNode({ node, activePath, expanded, toggle, searching, onOpen }: {
  node: Node; activePath?: string; expanded: Set<string>; toggle: (path: string) => void; searching: boolean; onOpen: (resource: WorkbenchResource) => void
}) {
  if (node.file) return <button type="button" className={`tree-file ${activePath === node.path ? 'active' : ''}`} aria-current={activePath === node.path ? 'page' : undefined}
    title={`${node.path}${node.file.reason ? ` · ${node.file.reason}` : ''}`} onClick={() => onOpen({ kind: 'file', ref: node.file!.path, title: node.name })}>
    <FileIcon name={node.name} /><span className="tree-name">{node.name}</span>{!node.file.editable && <span className="file-readonly" aria-label="只读"><WorkbenchIcon kind="lock" /></span>}
  </button>
  const open = searching || expanded.has(node.path)
  return <div className="tree-directory"><button type="button" className="tree-folder" aria-expanded={open} title={node.path} onClick={() => toggle(node.path)}>
    <svg className={`tree-chevron ${open ? 'open' : ''}`} viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
    <svg className="tree-folder-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h7l2 2h9v12H3z" /></svg><span className="tree-name">{node.name}</span>
  </button>{open && <div>{sorted([...node.children.values()]).map((child) => <TreeNode key={child.path} node={child} activePath={activePath} expanded={expanded} toggle={toggle} searching={searching} onOpen={onOpen} />)}</div>}</div>
}
export function FileEmptyState() {
  return <div className="file-open-empty"><svg viewBox="0 0 32 32" aria-hidden="true"><path d="M5 10h9l3 3h10v14H5z M9 6h10l3 3h6" /></svg><h2>打开文件</h2><p>从右侧项目目录中选择文件</p></div>
}
export function FileTree({ workspaceId, trustedLocal, activePath, onOpenResource }: {
  workspaceId: string | null; trustedLocal: boolean; activePath?: string; onOpenResource: (resource: WorkbenchResource) => void
}) {
  const [files, setFiles] = useState<ProjectFileEntry[]>([])
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [truncated, setTruncated] = useState(false)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    setFiles([]); setQuery(''); setExpanded(new Set()); setTruncated(false); setError(null)
  }, [workspaceId])
  useEffect(() => {
    if (!workspaceId || !trustedLocal) return
    let current = true; setBusy(true); setError(null)
    void listProjectFiles(workspaceId).then((response) => { if (current) { setFiles(response.files); setTruncated(response.truncated) } }).catch((reason: Error) => { if (current) setError(reason.message) }).finally(() => { if (current) setBusy(false) })
    return () => { current = false }
  }, [workspaceId, trustedLocal, revision])
  useEffect(() => {
    if (!activePath) return
    const parts = activePath.replace(/\\/g, '/').split('/').slice(0, -1)
    setExpanded((current) => new Set([...current, ...parts.map((_, index) => parts.slice(0, index + 1).join('/'))]))
  }, [activePath, workspaceId])
  if (!trustedLocal) return <div className="panel-empty"><h2>项目文件当前不可读取</h2><p>请从受信任的本地工作区启动文件编辑能力。</p></div>
  const matching = files.filter((file) => file.path.toLowerCase().includes(query.trim().toLowerCase()))
  return <div className="file-browser"><header className="file-browser-heading"><span>项目文件</span><button type="button" title="刷新文件目录" aria-label="刷新文件目录" disabled={busy} onClick={() => setRevision((value) => value + 1)}><WorkbenchIcon kind="refresh" /></button></header>
    <label className="file-search"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6" /><path d="m15 15 5 5" /></svg><input className="file-filter" aria-label="筛选项目文件" placeholder="筛选文件…" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
    {error && <div className="notice err" role="alert">{error}<button type="button" onClick={() => setRevision((value) => value + 1)}>重试</button></div>}
    {truncated && <div className="notice">已达到展示上限，其余文件可在本地终端查看。</div>}
    {busy && <div className="panel-loading" role="status">正在读取文件…</div>}
    {!busy && !error && !matching.length && <p className="panel-empty">{query.trim() ? '没有匹配的文件' : '没有可展示的文本文件'}</p>}
    <div className="file-tree">{fileTree(matching).map((node) => <TreeNode key={node.path} node={node} activePath={activePath} expanded={expanded}
      toggle={(path) => setExpanded((current) => { const next = new Set(current); if (next.has(path)) next.delete(path); else next.add(path); return next })}
      searching={!!query.trim()} onOpen={onOpenResource} />)}</div>
  </div>
}
