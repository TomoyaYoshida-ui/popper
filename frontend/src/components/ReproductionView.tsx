import type { PopperState } from '../types'

export function ReproductionView({ data, pending, onAction }: {
  data: PopperState; pending: boolean; onAction: (action: string) => Promise<boolean>
}) {
  const state = data.state as { phase?: string; verification?: { assessment?: { outcome?: string } } }
  const projection = data.research_projection
  const observation = projection?.observations[0]
  const decision = projection?.decisions[0]
  const graph = projection?.experiment_graph
  const outcome = decision?.action ?? state.verification?.assessment?.outcome
  return (
    <div className="page">
      <div className="section box">
        <div className="eyebrow">PAPER REPRODUCTION · UNIFIED RESEARCH KERNEL</div>
        <h1 style={{ fontSize: 24, marginTop: 8 }}>{data.paper?.title ?? '论文复现任务'}</h1>
        <p className="muted">{data.paper?.claim.text}</p>
        <div className={`notice ${graph?.integrity.ok ? '' : 'err'}`}>
          <strong>{graph?.integrity.ok ? '溯源完整' : '溯源存在断链'}</strong>
          <span className="muted" style={{ marginLeft: 8 }}>
            {graph?.nodes.length ?? 0} 节点 · {graph?.edges.length ?? 0} 条关系 · 阶段 {state.phase ?? '—'}
          </span>
        </div>
        <div className="row"><span className="muted">论文 / DOI</span><span>{data.paper?.year} · {data.paper?.doi}</span></div>
        <div className="row"><span className="muted">冻结数据</span><span>{data.protocol?.dataset.name ?? '—'}</span></div>
        <div className="row"><span className="muted">论文报告值</span><span className="metric">{data.paper?.claim.reported_value ?? '—'}</span></div>
        <div className="row"><span className="muted">复现观测值</span><span className="metric">{observation?.observed_value ?? '—'} {observation?.metric.unit ?? ''}</span></div>
        <div className="row"><span className="muted">绝对差 / 容差</span><span>{observation ? `${observation.absolute_difference} / ${observation.absolute_tolerance}` : '—'}</span></div>
        {outcome && <div className={`notice ${outcome === 'supports_reproduction' ? '' : 'err'}`}>
          核心裁定：{outcome === 'supports_reproduction' ? '结果在预注册容差内' : '未复现论文报告值'}
        </div>}
        {!outcome && <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          {state.phase === 'registered' && <button disabled={pending} onClick={() => void onAction('run')}>执行并核验</button>}
          {state.phase === 'executed' && <button disabled={pending} onClick={() => void onAction('verify')}>核验冻结产物</button>}
        </div>}
      </div>
      <div className="section box">
        <h2 style={{ fontSize: 16 }}>冻结产物</h2>
        {(data.artifacts ?? []).map((artifact) => (
          <div className="row" key={artifact.path}>
            <span>{artifact.path}</span>
            <span className="code">{artifact.sha256.slice(0, 16)}… · {artifact.bytes} B</span>
          </div>
        ))}
        {!(data.artifacts ?? []).length && <div className="muted">尚未产生复现产物。</div>}
      </div>
    </div>
  )
}
