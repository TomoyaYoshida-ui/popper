import { useEffect, useRef, useState } from 'react'
import { postWorkspaceOp } from '../api'
import type { Folder, Quest, Workspace } from '../types'

type Ctx = { x: number; y: number; folderId: string; taskId?: string } | null

function fmt(t: string): string {
  if (!t) return ''
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return ''
  const now = Date.now()
  const delta = Math.max(0, now - d.getTime())
  const min = Math.floor(delta / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr} 小时前`
  return `${Math.floor(hr / 24)} 天前`
}

/** QUESTS 侧栏：文件夹展开/折叠、⋯菜单、＋新建 Quest、重命名、移除、跨文件夹拖拽。全接真实后端。 */
export function QuestsSidebar({
  workspace,
  seedActive,
  reload,
  onOpenQuest,
  onOpenMaterialize,
}: {
  workspace: Workspace | null
  seedActive: boolean
  reload: () => void
  onOpenQuest: (title: string) => void
  onOpenMaterialize: () => void
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [ctx, setCtx] = useState<Ctx>(null)
  const [renaming, setRenaming] = useState<{ fid: string; name: string } | null>(null)
  const [dragId, setDragId] = useState<string | null>(null)
  const [overFid, setOverFid] = useState<string | null>(null)
  const foldTimer = useRef<number | null>(null)

  const folders = workspace?.folders ?? []
  const seedQuid = folders.length && folders[0].tasks.length ? folders[0].tasks[0].quid : null

  const run = async (op: Parameters<typeof postWorkspaceOp>[0]) => {
    try { await postWorkspaceOp(op) } finally { reload() }
  }

  const toggleFolder = (fid: string, ev?: React.MouseEvent) => {
    if (ev?.stopPropagation) ev.stopPropagation()
    if (renaming && renaming.fid === fid) return
    if (foldTimer.current) window.clearTimeout(foldTimer.current)
    foldTimer.current = window.setTimeout(() => {
      setOpen((o) => ({ ...o, [fid]: !o[fid] }))
    }, 160)
  }

  const onMore = (e: React.MouseEvent, folderId: string) => {
    e.stopPropagation()
    if (foldTimer.current) window.clearTimeout(foldTimer.current)
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect()
    setCtx({ x: Math.min(r.left, window.innerWidth - 214), y: Math.min(r.bottom + 6, window.innerHeight - 140), folderId })
  }

  const addQuest = (e: React.MouseEvent, fid: string) => {
    e.stopPropagation()
    if (foldTimer.current) window.clearTimeout(foldTimer.current)
    setOpen((o) => ({ ...o, [fid]: true }))
    run({ op: 'task_create', fid })
  }

  const quickAdd = () => {
    if (folders.length) {
      const fid = folders[0].fid
      setOpen((o) => ({ ...o, [fid]: true }))
      run({ op: 'task_create', fid })
    } else {
      onOpenMaterialize()
    }
  }

  const startRename = (fid: string, name: string) => setRenaming({ fid, name })

  const commitRename = () => {
    if (renaming) {
      if (renaming.name.trim()) run({ op: 'folder_rename', fid: renaming.fid, name: renaming.name.trim() })
    }
    setRenaming(null)
  }

  const ctxAction = (act: string) => {
    if (!ctx) return
    if (act === 'rename') startRename(ctx.folderId, folders.find((f) => f.fid === ctx.folderId)?.name ?? '')
    else if (act === 'remove') run({ op: 'folder_remove', fid: ctx.folderId })
    setCtx(null)
  }

  const toggleDone = (e: React.MouseEvent, q: Quest, f: Folder) => {
    e.stopPropagation()
    run({ op: 'task_toggle', fid: f.fid, quid: q.quid, done: !q.done })
  }

  const onDrop = (fid: string) => {
    setOverFid(null)
    if (dragId === fid) return
    if (!dragId) return
    // dragId 形如 "from:quid"
    const [fromFid, quid] = dragId.split(':')
    if (fromFid && quid && fromFid !== fid) {
      run({ op: 'task_move', from_fid: fromFid, quid, to_fid: fid })
      setOpen((o) => ({ ...o, [fid]: true }))
    }
    setDragId(null)
  }

  useEffect(() => {
    if (!ctx) return
    const close = () => setCtx(null)
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [ctx])

  return (
    <div>
      <button className="newq" onClick={quickAdd}>
        <span>创建 Quest</span><kbd>Ctrl N</kbd>
      </button>

      <div className="sec-h">
        <span className="t">QUESTS</span>
        <span className="tool"><span title="新建文件夹" onClick={() => run({ op: 'folder_create', name: '新建文件夹' })}>+</span></span>
      </div>

      {folders.map((f, fi) => (
        <div key={f.fid} className={`fgroup ${open[f.fid] !== false ? 'open' : ''} ${overFid === f.fid ? 'dragover' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setOverFid(f.fid) }}
          onDragLeave={() => setOverFid(null)}
          onDrop={() => onDrop(f.fid)}>
          <div className="folder" onClick={(e) => toggleFolder(f.fid, e)}>
            <span className="chev">▸</span>
            {renaming && renaming.fid === f.fid ? (
              <input className="rn" value={renaming.name} autoFocus
                onChange={(e) => setRenaming({ fid: f.fid, name: e.target.value })}
                onClick={(e) => e.stopPropagation()}
                onKeyDown={(e) => { if (e.key === 'Enter') commitRename(); if (e.key === 'Escape') setRenaming(null) }}
                onBlur={commitRename} />
            ) : (
              <>
                <span className="fico">▣</span>
                <span className="nm" onDoubleClick={() => startRename(f.fid, f.name)}>{f.name}</span>
                <span className="cnt">{f.tasks.length}</span>
                <span className="fops">
                  <span className="fop more" title="更多" onClick={(e) => onMore(e, f.fid)}>⋯</span>
                  <span className="fop add" title="新建 Quest" onClick={(e) => addQuest(e, f.fid)}>＋</span>
                </span>
              </>
            )}
          </div>
          <div className="ftasks">
            {f.tasks.map((q) => {
              const isSeed = fi === 0 && seedActive && q.quid === seedQuid
              return (
                <div key={q.quid}
                  className={`titem ${q.done ? 'done' : ''} ${isSeed ? 'run' : ''} ${dragId === `${f.fid}:${q.quid}` ? 'dragging' : ''}`}
                  draggable
                  onDragStart={() => setDragId(`${f.fid}:${q.quid}`)}
                  onDragEnd={() => setDragId(null)}
                  onClick={() => onOpenQuest(q.title)}>
                  <span className="dot" title={q.done ? '已完成' : '进行中'} onClick={(e) => toggleDone(e, q, f)} />
                  <span className="nm">{q.title}</span>
                  <span className="tm">{fmt(q.created)}</span>
                </div>
              )
            })}
          </div>
        </div>
      ))}

      {ctx && (
        <div className="ctx-menu" style={{ left: ctx.x, top: ctx.y }}>
          <button className="ctx-item" onClick={() => ctxAction('rename')}>
            <span className="ic">✎</span>编辑工作区
          </button>
          <button className="ctx-item" onClick={() => ctxAction('editor')}>
            <span className="ic">▣</span>在编辑器中打开
          </button>
          <div className="ctx-sep" />
          <button className="ctx-item danger" onClick={() => ctxAction('remove')}>
            <span className="ic">✕</span>从侧边栏移除
          </button>
        </div>
      )}
    </div>
  )
}