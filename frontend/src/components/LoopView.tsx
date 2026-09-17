import { useEffect, useState } from 'react'
import { fetchArbor, type ArborTree } from '../api'

export function LoopView() {
  const [data, setData] = useState<{ trees: ArborTree[]; runs: string[]; node_status_labels: Record<string, string> } | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    fetchArbor()
      .then((d) => { if (alive) { setData(d); setError(null) } })
      .catch((e) => { if (alive) setError((e as Error).message) })
    return () => { alive = false }
  }, [])

  const trees = data?.trees ?? []
  const statusLabels = data?.node_status_labels ?? {}

  return (
    <>
      <p className="eyebrow">HYPOTHESIS LOOP / 假设环</p>
      <h1>假设搜索环</h1>
      {error && <div className="notice err">{error}</div>}

      {!data && !error && <div className="notice">正在读取 Arbor 假设树产物…</div>}

      {data && trees.length === 0 && (
        <div className="notice">
          未发现假设树。运行 <span className="metric">popper vendor arbor-init</span> / <span className="metric">arbor-add</span> 后，
          真实假设节点会出现在这里（persistent hypothesis tree、frontier、evidence、merge gate）。
        </div>
      )}

      {trees.length > 0 && trees.map((tree) => {
        const nodes = tree.nodes ?? []
        const depthLabel = Math.max(...nodes.map((n) => n.depth ?? 0), 0)
        return (
          <div className="section box" key={tree.run_dir}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
              <h2 style={{ fontSize: 16 }}>{tree.run_dir}</h2>
              <span className="muted" style={{ fontSize: 12 }}>深度 {depthLabel} · 节点 {nodes.length}</span>
            </div>
            <p className="muted" style={{ fontSize: 13, margin: '6px 0 4px' }}>目标：{tree.run?.objective}</p>
            <div className="row">
              <span className="muted">预算轮次</span>
              <span className="metric">{tree.run?.cycles_used ?? 0} / {tree.run?.budget_cycles ?? '—'}</span>
            </div>
            {nodes.map((n) => (
              <div className="row" key={n.id}>
                <div>
                  <div><span className="code">{n.id}</span>{n.parent ? ` ← ${n.parent}` : ''}</div>
                  <div style={{ fontSize: 13 }}>{n.hypothesis}</div>
                </div>
                <span className="metric" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <span className={`st ${n.status}`} />
                  {statusLabels[n.status] ?? ''}
                </span>
              </div>
            ))}
          </div>
        )
      })}
    </>
  )
}
