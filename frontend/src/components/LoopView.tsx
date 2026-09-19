import { useEffect, useState } from 'react'
import { fetchArbor, type ArborTree, type ResearchRunState, type ResearchState } from '../api'

/** 单个研究 run 的只读投影卡（/api/research，真实 research.sqlite）。 */
function ResearchRunCard({ run, labels }: { run: ResearchRunState; labels: ResearchState['status_labels'] }) {
  if (!run.ok) {
    return (
      <div className="section box">
        <h2 style={{ fontSize: 16 }}>{run.run_dir}</h2>
        <div className="notice err">{run.reason}</div>
      </div>
    )
  }
  const study = run.study
  const budget = run.budget
  const hypotheses = run.hypotheses ?? []
  const revisions = run.revisions ?? []
  const observations = run.observations ?? []
  const decisions = run.decisions ?? []
  const reflections = run.reflections ?? []
  return (
    <div className="section box">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <h2 style={{ fontSize: 16 }}>{run.run_dir}</h2>
        <span className="muted" style={{ fontSize: 12 }}>
          {labels.study?.[study?.status ?? ''] ?? ''} · 状态版本 v{run.state_version ?? '—'} · 事件 {run.events_total ?? 0} 条
        </span>
      </div>
      <p className="muted" style={{ fontSize: 13, margin: '6px 0 4px' }}>问题：{study?.question}</p>
      <div className="row">
        <span className="muted">能力模式 / 策略</span>
        <span className="metric">{run.manifest?.capability_mode ?? '—'} · {run.manifest?.policy ?? '—'}</span>
      </div>
      <div className="row">
        <span className="muted">预算（已花费 / 上限）</span>
        <span className="metric">
          {budget ? `${budget.spent.toPrecision(3)} / ${budget.cap}` : '—'}
          {budget ? ` · 可用 ${budget.available.toPrecision(3)}` : ''}
        </span>
      </div>

      <h2 style={{ fontSize: 14, marginTop: 10 }}>假设（{hypotheses.length}）</h2>
      {hypotheses.length === 0 && <div className="notice">研究库中还没有假设。</div>}
      {hypotheses.map((h) => (
        <div className="row" key={h.entity_id}>
          <div>
            <div><span className="code">{h.entity_id}</span> · v{h.version} · {labels.hypothesis?.[h.status] ?? ''}</div>
            <div style={{ fontSize: 13 }}>{h.mechanism}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              适用：{h.applicability}｜预测 {h.predictions.length} 条｜证伪判据 {h.falsification.length} 条
            </div>
          </div>
        </div>
      ))}

      <h2 style={{ fontSize: 14, marginTop: 10 }}>修订（{revisions.length}）</h2>
      {revisions.length === 0 && <div className="muted" style={{ fontSize: 12 }}>尚无假设版本事件。</div>}
      {revisions.map((r) => (
        <div className="row" key={`${r.hypothesis_id}-v${r.version}`}>
          <div>
            <span className="code">{r.hypothesis_id}</span> v{r.version}
            {r.parent_version != null ? ` ← v${r.parent_version}` : ''}
          </div>
          <span className="metric" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            {labels.origin?.[r.origin] ?? ''} · seq {r.seq}
          </span>
        </div>
      ))}

      <h2 style={{ fontSize: 14, marginTop: 10 }}>观察（{observations.length}）</h2>
      {observations.length === 0 && <div className="muted" style={{ fontSize: 12 }}>尚无开发集观察；运行 popper research run 后出现。</div>}
      {observations.map((o) => (
        <div className="row" key={o.entity_id}>
          <div>
            <div><span className="code">{o.hypothesis_id}</span> · {o.scope}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              {o.value} {o.unit}｜不确定度 {o.uncertainty ?? '—'}｜run {o.run_id}｜{o.trust}
            </div>
          </div>
        </div>
      ))}

      <h2 style={{ fontSize: 14, marginTop: 10 }}>决策（{decisions.length}）</h2>
      {decisions.length === 0 && <div className="muted" style={{ fontSize: 12 }}>尚无决策记录。</div>}
      {decisions.map((d) => (
        <div className="row" key={d.entity_id}>
          <div>
            <div>{labels.action?.[d.action] ?? ''} <span className="muted" style={{ fontSize: 12 }}>（基于状态 v{d.state_version}）</span></div>
            <div style={{ fontSize: 13 }}>{d.rationale}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              证据 {d.observation_refs.length} 条｜{d.actor}{d.model ? ` · ${d.model}` : ''}
            </div>
          </div>
        </div>
      ))}

      <h2 style={{ fontSize: 14, marginTop: 10 }}>反思（{reflections.length}）</h2>
      {reflections.length === 0 && (
        <div className="muted" style={{ fontSize: 12 }}>
          尚无反思记录；接入模型策略（如 DeepSeek）且候选开发实验完成后由控制器写入。
        </div>
      )}
      {reflections.map((r) => (
        <div className="row" key={r.entity_id}>
          <div>
            <div>{labels.action?.[r.action] ?? ''}{r.next_hypothesis_id ? ` → ${r.next_hypothesis_id}` : ''}</div>
            <div style={{ fontSize: 13 }}>{r.rationale}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              替代解释：{r.alternative_explanation}｜证据 {r.evidence_refs.length} 条｜{r.actor}{r.model ? ` · ${r.model}` : ''}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

export function LoopView({ research }: { research: ResearchState | null }) {
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
  const researchRuns = research?.runs ?? []

  return (
    <>
      <p className="eyebrow">HYPOTHESIS LOOP / 假设环</p>
      <h1>假设搜索环</h1>
      {error && <div className="notice err">{error}</div>}

      {!research && <div className="notice">正在读取研究控制器状态…</div>}
      {research && researchRuns.length === 0 && (
        <div className="notice">
          未登记研究运行。用 <span className="metric">popper serve &lt;项目&gt; --research-run &lt;research-run目录&gt;</span> 重启工作台，
          research controller 自身的假设 / 观察 / 修订 / 决策 / 反思（只读 research.sqlite 投影）会出现在这里。
        </div>
      )}
      {researchRuns.map((run) => (
        <ResearchRunCard key={run.run_dir} run={run} labels={research?.status_labels ?? {}} />
      ))}

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
