import { useEffect, useState } from 'react'
import { fetchLiterature, type Literature } from '../api'

export function LitView() {
  const [data, setData] = useState<Literature | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    fetchLiterature()
      .then((d) => { if (alive) { setData(d); setError(null) } })
      .catch((e) => { if (alive) setError((e as Error).message) })
    return () => { alive = false }
  }, [])

  const papers = data?.papers ?? []

  return (
    <>
      <p className="eyebrow">LITERATURE / 文献库</p>
      <h1>文献库</h1>
      {error && <div className="notice err">{error}</div>}

      {!data && !error && <div className="notice">正在读取 paper-search 检索产物…</div>}

      {data && papers.length === 0 && (
        <div className="notice">
          未发现检索产物。运行 <span className="metric">popper vendor paper-search</span> 后，
          真实论文会自动出现在这里（跨源去重、[ref:] 锚点、Real/Potential/Hallucinated 三态）。
        </div>
      )}

      {papers.length > 0 && (
        <div className="section">
          <div className="row"><span className="muted">来源 run</span><span className="metric">{data?.runs.join(', ') || '—'}</span></div>
          <div className="row"><span className="muted">论文数</span><span className="metric">{papers.length}</span></div>
          {papers.map((p, i) => (
            <div className="box" key={i} style={{ margin: '12px 0 0' }}>
              <h3 style={{ fontSize: 15, marginBottom: 4 }}>{p.title}</h3>
              <p className="muted" style={{ fontSize: 12 }}>
                {p.authors?.join(', ')} · {p.venue || '—'} {p.year}
              </p>
              <p className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                {p.source} {p.arxiv_id ? `· arXiv:${p.arxiv_id}` : ''}
                {p.doi ? ` · DOI ${p.doi}` : ''}
                {p.citation_count != null ? ` · 引用 ${p.citation_count}` : ''}
                {p.relevance_score != null ? ` · 相关度 ${p.relevance_score}` : ''}
              </p>
              {p.url && <a className="muted" style={{ fontSize: 11 }} href={p.url} target="_blank" rel="noreferrer">{p.url}</a>}
            </div>
          ))}
        </div>
      )}
    </>
  )
}