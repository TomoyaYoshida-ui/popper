import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { cancelRevisionJob, executeInitialRevision, fetchRevisionJob, listProjectFiles, previewInitialRevision, previewProjectFile, readProjectFile, saveProjectFile, submitInitialRevision } from '../api'
import type { InitialRevisionCreated, InitialRevisionPreview, InitialRevisionSpec, ProjectFileEntry, ProjectFilePreview, ResearchState, RevisionExecutionJob } from '../api'

import { type EditorController } from '../workbench'
import { restoreEditorDraft, useEditorDraft } from '../useEditorDraft'
import { useConfirmation } from './ConfirmProvider'
const MonacoEditor = lazy(() => import('./MonacoEditor'))

export function CodeEditorView({ workspaceId, workspaceRoot, research, trustedLocal, onResearchChanged, onDirtyChange, path, onControllerChange }: {
  path?: string
  onControllerChange?: (controller: EditorController) => void
  workspaceId: string | null
  workspaceRoot: string | null
  research: ResearchState | null
  trustedLocal: boolean
  onResearchChanged?: () => void
  onDirtyChange?: (dirty: boolean) => void
}) {
  const confirm = useConfirmation()
  const [files, setFiles] = useState<ProjectFileEntry[]>([])
  const [active, setActive] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [sourceText, setSourceText] = useState('')
  const [sha256, setSha256] = useState<string | null>(null)
  const [draftBase, setDraftBase] = useState<{ text: string; sha: string | null }>({ text: '', sha: null })
  const [editable, setEditable] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [preview, setPreview] = useState<ProjectFilePreview | null>(null)
  const [truncated, setTruncated] = useState(false)
  const [revisionMode, setRevisionMode] = useState(false)
  const [selectedDesign, setSelectedDesign] = useState('')
  const [revisionPreview, setRevisionPreview] = useState<InitialRevisionPreview | null>(null)
  const [createdRevision, setCreatedRevision] = useState<InitialRevisionCreated | null>(null)
  const [executionJob, setExecutionJob] = useState<RevisionExecutionJob | null>(null)
  const generation = useRef(0)
  const actionInFlight = useRef(false)
  const [conflict, setConflict] = useState(false)
  const [mode, setMode] = useState<'edit' | 'diff'>('edit')
  const draftKey = workspaceId && active ? `code:${workspaceId}:${active}` : null
  const { storageError, clearDraft } = useEditorDraft(draftKey, text, draftBase.text, draftBase.sha, dirty, revisionMode)

  const candidates = (research?.runs ?? []).flatMap((run) => {
    if (!run.ok || !run.manifest || run.manifest.project !== workspaceRoot ||
        run.study?.status !== 'active' || run.state_version == null) return []
    return (run.designs ?? []).filter((design) => design.status === 'frozen' &&
      (run.hypotheses ?? []).some((hypothesis) => hypothesis.entity_id === design.hypothesis_id &&
        hypothesis.status === 'untested' && !hypothesis.entity_id.startsWith('H-control-')))
      .map((design) => ({ run, design }))
  })
  const revisionSpec = (): InitialRevisionSpec | null => {
    const selected = candidates.find(({ run, design }) =>
      `${run.manifest?.study_id}:${design.entity_id}` === selectedDesign)
    if (!selected || !active || !sha256 || selected.run.state_version == null) return null
    return { study_id: selected.run.manifest!.study_id,
      expected_state_version: selected.run.state_version,
      hypothesis_id: selected.design.hypothesis_id, design_id: selected.design.entity_id,
      path: active, expected_sha256: sha256, replacement: text }
  }

  useEffect(() => { onDirtyChange?.(dirty) }, [dirty, onDirtyChange])
  useEffect(() => {
    if (!executionJob || !['queued', 'running'].includes(executionJob.status)) return
    let current = true
    const timer = window.setInterval(() => {
      void fetchRevisionJob(executionJob.job_id).then((job) => {
        if (!current) return
        setExecutionJob(job)
        if (!['queued', 'running'].includes(job.status)) onResearchChanged?.()
      }).catch((cause: Error) => { if (current) setError(cause.message) })
    }, 1500)
    return () => { current = false; window.clearInterval(timer) }
  }, [executionJob, onResearchChanged])
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  useEffect(() => {
    if (!workspaceId || !trustedLocal) return
    if (path) { void open(path, true); return () => { generation.current++ } }
    let current = true
    setBusy(true); setError(null); setFiles([]); setActive(null); setText(''); setSourceText(''); setSha256(null); setPreview(null)
    setRevisionMode(false); setRevisionPreview(null); setSelectedDesign(''); setCreatedRevision(null); setExecutionJob(null)
    void listProjectFiles(workspaceId).then((result) => {
      if (!current) return
      setFiles(result.files); setTruncated(result.truncated)
    }).catch((cause: Error) => { if (current) setError(cause.message) })
      .finally(() => { if (current) setBusy(false) })
    return () => { current = false; generation.current++ }
  }, [workspaceId, trustedLocal, path])

  const open = async (path: string, force = false) => {
    if (!workspaceId || (!force && (busy || path === active))) return
    if (!force && dirty && !await confirm({ title: '放弃文件修改？', description: '切换文件会放弃当前未保存内容；取消则留在当前文件。', details: [{ label: '当前文件', value: active ?? path }], confirmLabel: '放弃并切换', danger: true })) return
    const request = ++generation.current
    setBusy(true); setError(null); setStatus(null); setDirty(false); setPreview(null)
    setRevisionMode(false); setRevisionPreview(null); setSelectedDesign(''); setCreatedRevision(null); setExecutionJob(null)
    try {
      const file = await readProjectFile(workspaceId, path)
      if (request !== generation.current) return
      let draft = null
      try { draft = await restoreEditorDraft(`code:${workspaceId}:${file.path}`) }
      catch { setStatus('本地草稿不可读取，已加载磁盘文件。') }
      if (request !== generation.current) return
      setActive(file.path); setText(draft?.content ?? file.content); setSourceText(file.content); setSha256(file.sha256); setEditable(file.editable)
      setDraftBase({ text: draft?.baseContent ?? file.content, sha: draft?.baseSha256 ?? file.sha256 })
      setDirty(!!draft && draft.content !== file.content)
      setRevisionMode(!file.editable && !!draft?.revisionMode)
      setConflict(!!draft && draft.baseSha256 !== file.sha256); setMode(draft && draft.baseSha256 !== file.sha256 ? 'diff' : 'edit')
      if (draft) setStatus(draft.baseSha256 === file.sha256 ? '已恢复未保存草稿。' : '磁盘文件已变化，请核对恢复草稿与当前文件的差异。')
    } catch (cause) {
      if (request === generation.current) setError((cause as Error).message)
    } finally {
      if (request === generation.current) setBusy(false)
    }
  }

  const review = async () => {
    if (actionInFlight.current) return
    if (!workspaceId || !active || !sha256 || !editable || !dirty || busy || conflict) return
    actionInFlight.current = true; setBusy(true); setError(null); setStatus(null)
    try { setPreview(await previewProjectFile(workspaceId, active, text, sha256)); setMode('diff') }
    catch (cause) { setPreview(null); setError((cause as Error).message) }
    finally { actionInFlight.current = false; setBusy(false) }
  }

  const save = async () => {
    if (actionInFlight.current) return false
    if (!workspaceId || !active || !sha256 || !editable || !dirty || busy || !preview?.changed ||
        preview.before_sha256 !== sha256 || conflict) return false
    actionInFlight.current = true; setBusy(true); setError(null); setStatus(null)
    try {
      const saved = await saveProjectFile(workspaceId, active, text, sha256)
      if (saved.sha256 !== preview.after_sha256) throw new Error('保存后的摘要与已审查差异不一致，请重新载入核对')
      await clearDraft()
      setSha256(saved.sha256); setDirty(false)
      setDraftBase({ text, sha: saved.sha256 })
      setSourceText(text)
      setPreview(null)
      setFiles((items) => items.map((item) => item.path === active ? { ...item, bytes: saved.bytes } : item))
      setStatus(`已写回 ${active}`); setMode('edit'); return true
    } catch (cause) {
      setError((cause as Error).message); return false
    } finally { actionInFlight.current = false; setBusy(false) }
  }

  const reviewRevision = async () => {
    if (actionInFlight.current) return
    const spec = revisionSpec()
    if (!spec || !dirty || busy || conflict) return
    actionInFlight.current = true; setBusy(true); setError(null); setStatus(null)
    try { setRevisionPreview(await previewInitialRevision(spec)); setMode('diff') }
    catch (cause) { setRevisionPreview(null); setError((cause as Error).message) }
    finally { actionInFlight.current = false; setBusy(false) }
  }

  const submitRevision = async () => {
    if (actionInFlight.current) return
    const spec = revisionSpec()
    if (!spec || !revisionPreview || busy) return
    actionInFlight.current = true; setBusy(true); setError(null); setStatus(null)
    try {
      const created = await submitInitialRevision(spec, revisionPreview.proposal_sha256)
      await clearDraft()
      setRevisionPreview(null); setDirty(false); setRevisionMode(false); setText(sourceText); setMode('edit')
      setCreatedRevision(created)
      setStatus(`已创建 ${created.revision_id}：不可变、尚未执行；原文件未改变。执行需要再次明确确认。`)
      onResearchChanged?.()
    } catch (cause) { setError((cause as Error).message) }
    finally { actionInFlight.current = false; setBusy(false) }
  }

  const executeRevision = async () => {
    if (!createdRevision || busy || executionJob || actionInFlight.current) return
    actionInFlight.current = true
    if (!await confirm({ title: '执行冻结修订？', description: '将运行冻结代码、消耗开发预算，并可能产生 Observation；失败也可能消费预算。', details: [{ label: '修订', value: createdRevision.revision_id }], confirmLabel: '确认执行' })) { actionInFlight.current = false; return }
    setBusy(true); setError(null)
    try { setExecutionJob(await executeInitialRevision(createdRevision.revision_id, createdRevision.manifest_sha256)) }
    catch (cause) { setError((cause as Error).message) }
    finally { actionInFlight.current = false; setBusy(false) }
  }
  const cancelExecution = async () => {
    if (!executionJob || busy || actionInFlight.current || !['queued', 'running'].includes(executionJob.status)) return
    actionInFlight.current = true; setBusy(true); setError(null)
    try { setExecutionJob(await cancelRevisionJob(executionJob.job_id)) }
    catch (cause) { setError((cause as Error).message) }
    finally { actionInFlight.current = false; setBusy(false) }
  }

  const discard = async () => {
    await clearDraft(); setText(sourceText); setDirty(false); setPreview(null); setRevisionPreview(null); setRevisionMode(false); setConflict(false); setMode('edit')
  }
  useEffect(() => {
    onControllerChange?.({ dirty, label: active ?? '项目文件', save: async () => {
      if (!dirty) return true
      if (!editable || conflict || busy) { setStatus('请先处理当前编辑草稿或创建 Revision，再关闭标签。'); return false }
      if (!preview) { await review(); return false }
      return await save()
    }, discard })
  })

  if (!trustedLocal) return <>
    <p className="eyebrow">PROJECT FILES / 项目代码</p><h1>项目代码</h1>
    <div className="notice">代码编辑仅在本机可信模式下开放。使用 --trusted-local 启动 Popper 后可查看和编辑登记工作区内的文本源码。</div>
  </>

  return <div className="code-editor-root">
    {!path && <><p className="eyebrow">PROJECT FILES / 项目代码</p><h1>项目代码</h1>
    <p className="muted">已冻结的实验输入只读；修改实验代码请创建 Revision。</p></>}
    {error && <div className="notice err">{error}</div>}
    {status && <div className="notice" role="status">{status}</div>}
    {storageError && <div className="notice err" role="alert">{storageError}</div>}
    {truncated && <div className="notice">文件数量达到浏览上限，仅展示部分文件。</div>}
    <div className="code-editor-layout" style={path ? undefined : { display: 'grid', gridTemplateColumns: 'minmax(180px, 28%) minmax(0, 1fr)', gap: 16 }}>
      {!path && <div className="section box" style={{ maxHeight: 600, overflow: 'auto' }}>
        <strong>文件</strong>
        {files.length === 0 && <p className="muted">{busy ? '正在读取文件列表…' : '没有可展示的文本源码'}</p>}
        {files.map((file) => <button key={file.path} className={`nav ${active === file.path ? 'active' : ''}`}
          title={file.reason ?? file.path} disabled={busy} onClick={() => void open(file.path)}
          style={{ display: 'block', width: '100%', textAlign: 'left', overflowWrap: 'anywhere' }}>
          {file.editable ? '✎' : '🔒'} {file.path}
        </button>)}
      </div>}
      <div className="code-editor-document">
        {active ? <>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
            <strong>{active}</strong><span className="muted">{editable ? '可编辑' : '只读 · 已冻结输入'}</span>
          </div>
          {!editable && revisionMode && <div className="notice" style={{ marginTop: 12 }}>
            这里编辑的是 Revision 草稿，不会覆盖冻结源码。选择已冻结设计，预览差异后才能创建不可变修订。
            <select value={selectedDesign} onChange={(event) => { setSelectedDesign(event.target.value); setRevisionPreview(null) }}
              disabled={busy} style={{ display: 'block', marginTop: 8, maxWidth: '100%' }}>
              <option value="">选择研究设计</option>
              {candidates.map(({ run, design }) => <option key={`${run.manifest?.study_id}:${design.entity_id}`}
                value={`${run.manifest?.study_id}:${design.entity_id}`}>
                {run.manifest?.study_id} / {design.entity_id} / {design.hypothesis_id}
              </option>)}
            </select>
          </div>}
          {conflict && <div className="notice err"><strong>恢复草稿与磁盘版本存在冲突</strong><p>差异视图以当前磁盘文件为基准。核对后可继续审查草稿，保存仍需服务端版本校验。</p>
            <button type="button" className="mini" onClick={() => { setDraftBase({ text: sourceText, sha: sha256 }); setConflict(false); setPreview(null); setRevisionPreview(null); setStatus('已选择以当前磁盘版本为基准继续审查草稿。') }}>保留草稿并继续审查</button>
            <button type="button" className="mini" onClick={() => { void discard() }}>使用磁盘版本</button></div>}
          <div className="editor-mode-bar"><div className="editor-switch"><button type="button" className={mode === 'edit' ? 'active' : ''} onClick={() => setMode('edit')}>编辑</button>
            <button type="button" className={mode === 'diff' ? 'active' : ''} onClick={() => setMode('diff')}>差异</button></div>
            <span>{dirty ? '未保存' : '已保存'} · Ctrl + S 查看差异 / 保存</span></div>
          <div className="editor-canvas"><Suspense fallback={<div className="panel-loading">正在加载代码编辑器…</div>}>
            <MonacoEditor path={active} value={text} original={mode === 'diff' ? sourceText : undefined}
              readOnly={busy || (!editable && !revisionMode) || !sha256}
              onChange={(next) => { if (next !== text) { setText(next); setDirty(next !== sourceText); setPreview(null); setRevisionPreview(null) } }}
              onSave={() => { if (preview?.changed) void save(); else void review() }} />
          </Suspense></div>
          <div className="actions">
            {createdRevision && !executionJob && <button className="btn primary" disabled={busy}
              onClick={() => void executeRevision()}>确认并执行 {createdRevision.revision_id}</button>}
            {!editable && !revisionMode && <button className="btn" disabled={busy || !sha256 || candidates.length === 0}
              onClick={() => { setRevisionMode(true); setSelectedDesign(''); setRevisionPreview(null) }}>
              从冻结源码创建 Revision 草稿
            </button>}
            {!editable && revisionMode && <>
              <button className="btn" disabled={busy || !dirty || !revisionSpec()} onClick={() => void reviewRevision()}>
                预览 Revision 差异
              </button>
              <button className="btn primary" disabled={busy || !revisionPreview || !revisionSpec()} onClick={() => void submitRevision()}>
                确认并创建 Revision
              </button>
            </>}
            <button className="btn" disabled={busy || !editable || !dirty || !sha256 || conflict} onClick={() => void review()}>
              查看差异
            </button>
            <button className="btn primary" disabled={busy || !editable || !dirty || !preview?.changed || conflict} onClick={() => void save()}>
              {busy ? '处理中…' : '确认差异并写回'}
            </button>
            <button className="btn" disabled={busy} onClick={async () => {
              if (dirty && !await confirm({ title: '重新载入磁盘版本？', description: '当前未保存的编辑将被放弃，磁盘文件不会被改写。', details: [{ label: '文件', value: active ?? '' }], confirmLabel: '放弃并载入', danger: true })) return
              void clearDraft().then(() => open(active, true))
            }}>重新载入</button>
          </div>
          {executionJob && <div className="notice">
            执行作业 {executionJob.job_id}：{executionJob.status} / {executionJob.phase} / {executionJob.progress}%
            {executionJob.error && <span> · {executionJob.error}</span>}
            {executionJob.run_id && <span> · Run {executionJob.run_id}</span>}
            {['queued', 'running'].includes(executionJob.status) && <button className="mini" type="button"
              disabled={busy} onClick={() => void cancelExecution()}>取消执行</button>}
          </div>}
          {preview && <div className="section box" style={{ marginTop: 12 }}>
            <strong>写入前差异 · {preview.path}</strong>
            <p className="muted">原摘要 {preview.before_sha256.slice(0, 12)} → 新摘要 {preview.after_sha256.slice(0, 12)}。磁盘文件若已变化，写回会拒绝。</p>
            <p className="muted">{preview.changed ? '差异已在编辑器中显示，核对后可确认写回。' : '内容没有变化。'}</p>
          </div>}
          {revisionPreview && <div className="section box" style={{ marginTop: 12 }}>
            <strong>Revision 提案差异 · {revisionPreview.design_id}</strong>
            <p className="muted">{revisionPreview.effect}。源码或研究状态变化后，提交会拒绝。</p>
            <p className="muted">差异已在编辑器中显示；确认后创建不可变 Revision。</p>
          </div>}
        </> : <div className="panel-empty">
          <p className="muted" role={busy || (path && !error) ? 'status' : undefined}>{busy ? '正在读取文件…' : error ? '文件未能读取，请核对错误后重试。' : path ? '正在打开文件…' : '从项目目录中选择文件。'}</p>
          {error && path && <button className="btn" type="button" disabled={busy} onClick={() => void open(path, true)}>重试读取文件</button>}
        </div>}
      </div>
    </div>
  </div>
}
