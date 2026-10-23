import { useRef, useState } from 'react'
import { fileUrl, materialize, type MaterializeDisclosure, type MaterializeTemplate } from '../api'
import type { Deliverable, Section } from '../types'
import { useConfirmation } from './ConfirmProvider'

export function MaterializeDialog({
  deliverables,
  validTemplates,
  validDisclosures,
  disclosureLabels,
  onClose,
  onReload,
}: {
  deliverables: Deliverable[]
  validTemplates: string[]
  validDisclosures: string[]
  disclosureLabels: Record<string, string>
  onClose: () => void
  onReload: () => void
}) {
  const confirm = useConfirmation()
  const inFlight = useRef(false)
  const [title, setTitle] = useState('')
  const [abstract, setAbstract] = useState('')
  const [sections, setSections] = useState<Section[]>([{ heading: '', body: '' }])
  const [template, setTemplate] = useState<MaterializeTemplate>('md')
  const [disclosure, setDisclosure] = useState<MaterializeDisclosure>('nature')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<Deliverable | null>(null)

  const patchSection = (i: number, key: keyof Section, value: string) =>
    setSections((s) => s.map((sec, idx) => (idx === i ? { ...sec, [key]: value } : sec)))

  const submit = async () => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const clean = sections.filter((s) => s.heading.trim() || s.body.trim())
      const res = await materialize(
        { title: title.trim() || 'Untitled', abstract: abstract.trim(), sections: clean },
        template,
        disclosure,
      )
      setResult(res.item)
      onReload()
    } catch (e) {
      setResult(null)
      setError((e as Error).message)
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  const close = async () => {
    if (inFlight.current) return
    if (!result && (title.trim() || abstract.trim() || sections.some((item) => item.heading.trim() || item.body.trim()))
      && !await confirm({ title: '放弃导出稿件输入？', description: '尚未生成的标题、摘要与章节将被丢弃。已有稿件及证据绑定快照不受影响。', confirmLabel: '放弃并关闭', danger: true })) return
    onClose()
  }

  return (
    <div className="mask" onClick={() => { void close() }}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="export-manuscript-title" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2 id="export-manuscript-title">生成可编辑导出稿件</h2>
          <button className="modal-close" disabled={busy} aria-label="关闭导出稿件" onClick={() => { void close() }}>✕</button>
        </div>

        <p className="notice">这是独立导出产物，不是证据绑定快照，也不是自动写论文。含数字结论或科学主张时须带 [[claim:]] / [[ev:]] / [[ref:]]，且 observation / LIT- / SCOOP- / FT- id 须在当前 study 可引用；门禁失败不会生成成功产物。</p>

        <div className="fld">
          <label>标题</label>
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="稿件标题" />
        </div>
        <div className="fld">
          <label>摘要 Abstract</label>
          <textarea value={abstract} onChange={(e) => setAbstract(e.target.value)} placeholder="摘要内容" />
        </div>

        <div className="fld">
          <label>章节（自动排序为第 N 节）</label>
          {sections.map((sec, i) => (
            <div className="fld-row" key={i}>
              <input placeholder={`章节 ${i + 1} 标题`} value={sec.heading}
                onChange={(e) => patchSection(i, 'heading', e.target.value)} />
              <textarea placeholder="正文" value={sec.body}
                onChange={(e) => patchSection(i, 'body', e.target.value)} />
              <button className="btn ghost" onClick={() => setSections((s) => s.filter((_, x) => x !== i))}>✕</button>
            </div>
          ))}
          <button className="btn ghost" style={{ alignSelf: 'flex-start' }}
            onClick={() => setSections((s) => [...s, { heading: '', body: '' }])}>＋ 添加章节</button>
        </div>

        <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap' }}>
          <div className="fld">
            <label>模板</label>
            <select value={template} onChange={(e) => setTemplate(e.target.value as MaterializeTemplate)}>
              {validTemplates.map((t) => <option key={t} value={t}>{t.toUpperCase()}</option>)}
            </select>
          </div>
          <div className="fld">
            <label>AI 使用披露</label>
            <select value={disclosure ?? '__none__'}
              onChange={(e) => setDisclosure(e.target.value === '__none__' ? null : e.target.value as MaterializeDisclosure)}>
              <option value="__none__">不附加披露声明</option>
              {validDisclosures.map((d) => <option key={d} value={d}>{disclosureLabels[d] ?? ''}</option>)}
            </select>
          </div>
        </div>

        {error && <div className="notice err">{error}</div>}

        <div className="actions">
          <button className="btn primary" disabled={busy} onClick={submit}>
            {busy ? '正在生成…' : '生成导出稿件'}
          </button>
        </div>

        {result && !error && (
          <div className="box" style={{ marginTop: 8 }}>
            <div className="row"><span className="muted">已生成（写作门禁已通过）</span>
              <span className="metric">{result.filename}</span></div>
            <div className="row"><span className="muted">模板 / 披露</span>
              <span className="metric">{result.template} · {result.disclosure ?? '无'}</span></div>
            <div className="row"><span className="muted">大小</span>
              <span className="metric">{(result.bytes / 1024).toFixed(1)} KB</span></div>
            <a className="btn primary" style={{ display: 'inline-block', marginTop: 10 }}
              href={fileUrl(result.id)} download>下载 {result.filename}</a>
          </div>
        )}

        {deliverables.length > 0 && (
          <div className="section">
            <h3 style={{ fontSize: 15, marginBottom: 8 }}>已有独立导出产物</h3>
            {deliverables.map((d) => (
              <div className="row" key={d.id}>
                <div>
                  <span className="metric">{d.filename}</span>
                  <small className="muted" style={{ marginLeft: 8 }}>{d.template} · {(d.bytes / 1024).toFixed(1)} KB</small>
                </div>
                <a href={fileUrl(d.id)} download>下载</a>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
