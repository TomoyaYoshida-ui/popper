import { useCallback, useEffect, useState } from 'react'
import { fetchFileText, saveFileText } from '../api'
import type { Deliverable } from '../types'

const EDITABLE = (name: string) => /\.(md|tex|json)$/i.test(name)

export function EditorView({ deliverables, templateLabels, disclosureLabels, onReload }: {
  deliverables: Deliverable[]
  templateLabels: Record<string, string>
  disclosureLabels: Record<string, string>
  onReload?: () => void
}) {
  const editable = deliverables.filter((d) => EDITABLE(d.filename))
  const [active, setActive] = useState<string | null>(editable[0]?.id ?? null)
  const [text, setText] = useState('')
  const [dirty, setDirty] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const item = editable.find((d) => d.id === active) ?? null
  const loading = !!item

  const load = useCallback(async (id: string) => {
    setBusy(true); setError(null); setStatus(null); setDirty(false)
    try {
      const body = await fetchFileText(id)
      setText(body)
    } catch (e) {
      setError((e as Error).message); setText('')
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => { if (active) load(active) }, [active, load])

  const save = async () => {
    if (!item || !dirty) return
    setBusy(true); setError(null); setStatus(null)
    try {
      await saveFileText(item.id, text)
      setDirty(false); setStatus(`已写回 ${item.filename}`)
      onReload?.()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (deliverables.length === 0) {
    return (
      <>
        <p className="eyebrow">MANUSCRIPT EDITOR / 稿件编辑器</p>
        <h1>稿件编辑器</h1>
        <div className="notice">
          尚未物化任何稿件。请先在「启动台 → 成果写作」或产物面板创建一份可交付稿件，再回到这里编辑真实文件。
        </div>
      </>
    )
  }

  return (
    <>
      <p className="eyebrow">MANUSCRIPT EDITOR / 稿件编辑器</p>
      <h1>稿件编辑器</h1>

      {editable.length === 0 && (
        <div className="notice">
          当前仅有不可编辑的二进制产物（docx/pdf）。请物化一份 md/tex 稿件后即可在线编辑。
        </div>
      )}

      {editable.length > 0 && (
        <>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', margin: '14px 0' }}>
            {editable.map((d) => (
              <button key={d.id} className={`nav ${active === d.id ? 'active' : ''}`}
                onClick={() => setActive(d.id)}>
                {d.filename}
              </button>
            ))}
          </div>

          {item && (
            <div className="section box">
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
              <div className="fld">
                <textarea
                  value={text}
                  onChange={(e) => { setText(e.target.value); setDirty(true) }}
                  spellCheck={false}
                  style={{ fontFamily: 'var(--mono)', minHeight: 480, whiteSpace: 'pre-wrap' }}
                />
              </div>
            </div>
          )}

          {error && <div className="notice err">{error}</div>}
          {status && <div className="notice" style={{ borderColor: 'var(--ok)' }}>{status}</div>}

          <div className="actions">
            <button className="btn primary" disabled={busy || !item || !dirty} onClick={save}>
              {busy ? '保存中…' : '保存写回 /api/file'}
            </button>
            <span className="muted" style={{ alignSelf: 'center', fontSize: 12 }}>
              {loading ? '已加载真实物化文件' : '—'}
            </span>
          </div>
        </>
      )}
    </>
  )
}
