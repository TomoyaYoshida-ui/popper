import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { fetchFileText, saveFileText } from '../api'
import type { Deliverable } from '../types'

import { type EditorController, type WorkbenchResource } from '../workbench'
import { restoreEditorDraft, useEditorDraft } from '../useEditorDraft'
import { useConfirmation } from './ConfirmProvider'
import { Markdown } from './Markdown'
const MonacoEditor = lazy(() => import('./MonacoEditor'))
const EDITABLE = (name: string) => /\.(md|tex|json)$/i.test(name)

export function EditorView({ deliverables, templateLabels, disclosureLabels, onReload, onDirtyChange, itemId, workspaceKey = 'local', onControllerChange, onOpenResource }: {
  itemId?: string; workspaceKey?: string
  onControllerChange?: (controller: EditorController) => void
  onOpenResource?: (resource: WorkbenchResource) => void
  deliverables: Deliverable[]
  templateLabels: Record<string, string>
  disclosureLabels: Record<string, string>
  onReload?: () => void
  onDirtyChange?: (dirty: boolean) => void
}) {
  const confirm = useConfirmation()
  const editable = deliverables.filter((d) => EDITABLE(d.filename))
  const [active, setActive] = useState<string | null>(itemId ?? editable[0]?.id ?? null)
  const [text, setText] = useState('')
  const [sourceText, setSourceText] = useState('')
  const [conflict, setConflict] = useState(false)
  const [mode, setMode] = useState<'edit' | 'preview' | 'diff'>('edit')
  const [sha256, setSha256] = useState<string | null>(null)
  const [draftBase, setDraftBase] = useState<{ text: string; sha: string | null }>({ text: '', sha: null })
  const [dirty, setDirty] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const loadGeneration = useRef(0)
  const actionInFlight = useRef(false)

  const item = editable.find((d) => d.id === active) ?? null
  const loading = !!item
  const { storageError, clearDraft } = useEditorDraft(active ? `manuscript:${workspaceKey}:${active}` : null, text, draftBase.text, draftBase.sha, dirty)

  const load = useCallback(async (id: string, restore = true) => {
    const generation = ++loadGeneration.current
    setBusy(true); setError(null); setStatus(null); setDirty(false); setSha256(null)
    try {
      const body = await fetchFileText(id)
      if (generation !== loadGeneration.current) return
      let draft = null
      if (restore) try { draft = await restoreEditorDraft(`manuscript:${workspaceKey}:${id}`) } catch { setStatus('本地草稿不可读取，已加载磁盘文件。') }
      if (generation !== loadGeneration.current) return
      setText(draft?.content ?? body.text); setSourceText(body.text); setSha256(body.sha256)
      setDraftBase({ text: draft?.baseContent ?? body.text, sha: draft?.baseSha256 ?? body.sha256 })
      setDirty(!!draft && draft.content !== body.text); setConflict(!!draft && draft.baseSha256 !== body.sha256)
      if (draft) { setMode(draft.baseSha256 === body.sha256 ? 'edit' : 'diff'); setStatus(draft.baseSha256 === body.sha256 ? '已恢复未保存稿件。' : '磁盘稿件已有变化，请检查差异。') }
    } catch (e) {
      if (generation !== loadGeneration.current) return
      setError((e as Error).message); setText(''); setSha256(null)
    } finally {
      if (generation === loadGeneration.current) setBusy(false)
    }
  }, [workspaceKey])

  useEffect(() => { if (active) void load(active); return () => { loadGeneration.current++ } }, [active, load])
  useEffect(() => { onDirtyChange?.(dirty) }, [dirty, onDirtyChange])
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const choose = async (id: string) => {
    const target = editable.find((entry) => entry.id === id)
    if (onOpenResource && target) { onOpenResource({ kind: 'manuscript', ref: id, title: target.filename }); return }
    if (id === active || busy) return
    if (dirty && !await confirm({ title: '切换导出稿件？', description: '将放弃当前稿件未保存内容；证据绑定快照不会受影响。', confirmLabel: '放弃并切换', danger: true })) return
    setActive(id)
  }

  const save = async (): Promise<boolean> => {
    if (!item || !sha256 || busy || conflict || actionInFlight.current) return false
    if (!dirty) return true
    actionInFlight.current = true; setBusy(true); setError(null); setStatus(null)
    try {
      const nextSha256 = await saveFileText(item.id, text, sha256)
      await clearDraft(); setSourceText(text)
      setSha256(nextSha256)
      setDraftBase({ text, sha: nextSha256 })
      setDirty(false); setStatus(`已写回 ${item.filename}`)
      onReload?.(); return true
    } catch (e) {
      setError((e as Error).message); return false
    } finally {
      actionInFlight.current = false
      setBusy(false)
    }
  }

  const discard = async () => { await clearDraft(); setText(sourceText); setDirty(false); setConflict(false); setMode('edit') }
  useEffect(() => { onControllerChange?.({ dirty, label: item?.filename ?? '稿件', save, discard }) })

  if (deliverables.length === 0) {
    return (
      <>
        <p className="eyebrow">MANUSCRIPT EDITOR / 稿件编辑器</p>
        <h1>稿件编辑器</h1>
        <div className="notice">
          尚无可编辑稿件。可在 Study 中生成证据绑定草稿，或通过产物导出创建可编辑稿件。
        </div>
      </>
    )
  }

  return (
    <>
      {!itemId && <><p className="eyebrow">MANUSCRIPT EDITOR</p><h1>稿件编辑器</h1></>}

      {editable.length === 0 && (
        <div className="notice">
          当前仅有不可编辑的二进制产物（docx/pdf）。请物化一份 md/tex 稿件后即可在线编辑。
        </div>
      )}

      {editable.length > 0 && (
        <>
          {!itemId && <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', margin: '14px 0' }}>
            {editable.map((d) => (
              <button key={d.id} className={`nav ${active === d.id ? 'active' : ''}`}
                disabled={busy} onClick={() => choose(d.id)}>
                {d.filename}
              </button>
            ))}
          </div>}

          {item && (
            <div className="manuscript-document">
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', marginBottom: 10 }}>
                <div>
                  <span className="metric" style={{ fontSize: 15 }}>{item.filename}</span>
                  <span className="muted" style={{ marginLeft: 10 }}>
                    {templateLabels[item.template] ?? ''} · {(item.bytes / 1024).toFixed(1)} KB
                  </span>
                </div>
                <div className="adj-ops">
                  <span className="muted" style={{ fontSize: 12 }}>
                    AI 披露：{item.disclosure ? (disclosureLabels[item.disclosure] ?? '') : '未附加'}
                  </span>
                </div>
              </div>
              {item.disclosure === null && (
                <div className="notice" style={{ margin: '0 0 10px' }}>
                  本文未附加 AI 使用披露声明。保存不会自动改写，请按需在文末补充。
                </div>
              )}
              {conflict && <div className="notice err"><strong>恢复草稿与磁盘版本存在冲突</strong><p>请核对当前磁盘文件与草稿的差异。</p><button type="button" className="mini" onClick={() => { setDraftBase({ text: sourceText, sha: sha256 }); setConflict(false) }}>保留草稿并继续审查</button><button type="button" className="mini" onClick={() => { void discard() }}>使用磁盘版本</button></div>}
              <div className="editor-mode-bar"><div className="editor-switch">{(['edit', 'preview', 'diff'] as const).map((key) => <button type="button" className={mode === key ? 'active' : ''} key={key} onClick={() => setMode(key)}>{{ edit: '编辑', preview: '预览', diff: '差异' }[key]}</button>)}</div><span>{dirty ? '未保存' : '已保存'}</span></div>
              {mode === 'preview' ? <div className="manuscript-preview">{item.filename.endsWith('.md') ? <Markdown text={text} /> : <pre>{text}</pre>}
                {onOpenResource && <button type="button" className="mini" onClick={() => onOpenResource({ kind: 'evidence' })}>查看研究证据 →</button>}</div>
                : <div className="editor-canvas"><Suspense fallback={<div className="panel-loading">正在加载稿件编辑器…</div>}><MonacoEditor path={item.filename} value={text} original={mode === 'diff' ? sourceText : undefined} readOnly={busy || !sha256} onChange={(value) => { if (value !== text) { setText(value); setDirty(value !== sourceText) } }} onSave={() => { void save() }} /></Suspense></div>}
            </div>
          )}

          {error && <div className="notice err" role="alert">{error}</div>}{storageError && <div className="notice err">{storageError}</div>}
          {status && <div className="notice" style={{ borderColor: 'var(--ok)' }}>{status}</div>}

          <div className="actions">
            <button className="btn primary" disabled={busy || !item || !dirty || !sha256 || conflict} onClick={save}>
              {busy ? '保存中…' : '保存稿件'}
            </button>
            <button className="btn" disabled={busy || !item} onClick={async () => {
              if (dirty && !await confirm({ title: '重新载入导出稿件？', description: '将放弃当前未保存内容，读取磁盘版本。', details: [{ label: '稿件', value: item?.filename ?? '' }], confirmLabel: '放弃并载入', danger: true })) return
              if (item) void clearDraft().then(() => load(item.id, false))
            }}>重新载入磁盘版本</button>
            <span className="muted" style={{ alignSelf: 'center', fontSize: 12 }}>
              {loading ? '已加载真实物化文件' : '—'}
            </span>
          </div>
        </>
      )}
    </>
  )
}
