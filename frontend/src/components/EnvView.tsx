import type { PopperState } from '../types'

export function EnvView({ data, onAction }: {
  data: PopperState
  onAction: (a: string) => void
}) {
  const st = data.state as { phase: string; spec: { metric: { name: string; direction: string }; budget: number; seeds?: number[] }; claim: { delta: number; threshold: number; status: string } | null; environment?: Record<string, unknown>; evaluator_id?: string }
  const results = data.results

  return (
    <>
      <p className="eyebrow">ENVIRONMENT / 复现与重放</p>
      <h1>实验环境</h1>

      <div className="section box">
        <h2 style={{ fontSize: 16 }}>执行与评估</h2>
        <div className="row"><span className="muted">评估器</span><span className="metric">{st.evaluator_id ?? '—'}</span></div>
        <div className="row"><span className="muted">指标</span><span className="metric">{st.spec.metric.name}（{st.spec.metric.direction === 'min' ? '越低越好' : '越高越好'}）</span></div>
        <div className="row"><span className="muted">候选预算</span><span className="metric">{st.spec.budget}</span></div>
        <div className="row"><span className="muted">种子</span><span className="metric">{st.spec.seeds?.length ?? '—'}</span></div>
        <div className="row"><span className="muted">已完成运行</span><span className="metric">{results.length}</span></div>
      </div>

      <div className="section box">
        <h2 style={{ fontSize: 16 }}>执行环境（输入指纹）</h2>
        {st.environment ? (
          <pre style={{ fontFamily: 'ui-monospace,monospace', fontSize: 12, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(st.environment, null, 2)}</pre>
        ) : (
          <p className="muted">实验状态未记录环境细节（尚未执行或为复现任务）。</p>
        )}
      </div>

      <div className="section box">
        <h2 style={{ fontSize: 16 }}>复现与重放</h2>
        {st.claim ? (
          <>
            <div className="metric" style={{ fontSize: 22, margin: '8px 0' }}>δ = {st.claim.delta.toPrecision(4)}</div>
            <p className="muted">阈值 {st.claim.threshold} · {st.claim.status === 'supports_threshold' ? '达到预注册改善阈值' : '证据不足'}</p>
          </>
        ) : (
          <p className="muted">尚未形成结论。</p>
        )}
        <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
          <button className="nav active" onClick={() => onAction('replay')}>校验证据并重算</button>
          <button className="nav" onClick={() => onAction('report')}>生成报告</button>
        </div>
        <p className="muted" style={{ fontSize: 12, marginTop: 12 }}>
          阶段：{data.phases?.[st.phase] ?? st.phase} · 重放离线重算指标，不重跑实验或调用模型。
        </p>

        {data.job && (
          <div className="box" style={{ marginTop: 14, padding: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="muted" style={{ fontSize: 12 }}>最近动作</span>
              <span className={`st-chip ${data.job.status === 'failed' ? 'wait' : 'run'}`}>
                <i />{data.job.action ?? '—'} · {(data.job.status ?? 'idle')}
              </span>
            </div>
            {data.job.status === 'failed' && data.job.error && (
              <div className="notice err" style={{ margin: '8px 0 0' }}>{data.job.error}</div>
            )}
          </div>
        )}
      </div>
    </>
  )
}