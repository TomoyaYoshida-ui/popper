import type { WorkbenchResource } from '../workbench'

type RecordValue = Record<string, unknown>
const object = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
const number = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null
const count = (value: unknown): string => number(value) != null && Number.isInteger(value) && (value as number) >= 0 ? String(value) : '未提供'
const measured = (value: unknown): string => {
  const finite = number(value)
  return finite == null ? '未提供' : finite === 0 ? '0' : finite.toPrecision(6)
}
const executionLabels: Record<string, string> = { succeeded: '执行成功', failed: '执行失败', interrupted: '执行中断', cancelled: '已取消', queued: '等待执行', running: '正在执行', completed: '执行完成' }
const systemLabels: Record<string, string> = { harmonic: '谐振子', pendulum: '非线性摆' }

/** Read-only development evidence, not a second source of research eligibility. */
export function developmentDiagnostics(value: unknown): RecordValue | null {
  const evidence = object(value)
  return evidence.scope === 'dev' && typeof evidence.study_id === 'string' && typeof evidence.job_id === 'string' ? evidence : null
}

export function DevelopmentDiagnostics({ value, studyId, jobId, currentStateVersion, compact = false, onOpenResource }: {
  value: unknown; studyId?: string; jobId?: string; currentStateVersion?: number | null; compact?: boolean
  onOpenResource: (resource: WorkbenchResource) => void
}) {
  const evidence = developmentDiagnostics(value)
  if (!evidence || (studyId && evidence.study_id !== studyId) || (jobId && evidence.job_id !== jobId)) return null
  const candidate = object(evidence.candidate), control = object(evidence.control), comparison = object(evidence.comparison)
  const budget = object(evidence.current_budget), remaining = object(budget.candidate_development).remaining
  const snapshotVersion = number(evidence.state_version)
  const stale = currentStateVersion != null && snapshotVersion !== currentStateVersion
  const threshold = comparison.development_threshold_met
  const results = [['候选', candidate], ['固定基线', control]] as const
  return <section className="development-diagnostics" aria-label="开发集诊断">
    <h3>开发集诊断</h3>
    <p className="muted">仅开发集 · {snapshotVersion == null ? '状态版本未提供' : `状态版本 ${snapshotVersion}`} · 不构成独立确认或 Claim。</p>
    {stale && <p className="notice warn" role="status">历史诊断快照：研究版本已变化，预算与资格需重新读取；以下数值仅对应此记录。</p>}
    <dl className={compact ? 'tool-summary' : 'resource-facts'}>
      <div><dt>候选轨迹误差</dt><dd>{measured(candidate.trajectory_error)}</dd></div>
      <div><dt>固定基线轨迹误差</dt><dd>{measured(control.trajectory_error)}</dd></div>
      <div><dt>相对改善</dt><dd>{number(comparison.relative_improvement) == null ? '未提供' : `${measured((comparison.relative_improvement as number) * 100)}%`}</dd></div>
      <div><dt>开发阈值判定</dt><dd>{threshold === true ? '达到开发阈值（仍需独立确认）' : threshold === false ? '未达到开发阈值' : '尚无可核验判定'}</dd></div>
      <div><dt>候选执行状态</dt><dd>{typeof candidate.execution_status === 'string' ? executionLabels[candidate.execution_status] ?? candidate.execution_status : '未提供'}</dd></div>
      <div><dt>执行完成案例</dt><dd>{count(candidate.execution_cases)}</dd></div>
      <div><dt>轨迹格式有效案例</dt><dd>{count(candidate.trajectory_valid_cases)}</dd></div>
      <div><dt>可信 RHS 查询</dt><dd>{count(candidate.trusted_rhs_queries)}</dd></div>
      {!stale && remaining != null && <div><dt>候选开发次数剩余</dt><dd>{count(remaining)}</dd></div>}
    </dl>
    <p className="muted">执行完成、轨迹格式有效不等于科研达标。能量漂移仅覆盖预定采样点，不能代替轨迹或相位准确性；RHS 查询数不等于全部计算成本。</p>
    <div className="resource-links">{results.map(([name, result]) => typeof result.observation_id === 'string' && <button key={name} type="button" title={result.observation_id}
      onClick={() => onOpenResource({ kind: 'observation', ref: result.observation_id as string, studyId: evidence.study_id as string, title: `${name}开发观察` })}>{name}开发观察 ↗</button>)}</div>
    <details className="resource-technical"><summary>逐系统与案例诊断</summary>
      {results.map(([name, result]) => <div key={name}><h4>{name}</h4>
        {Object.entries(object(result.systems)).map(([system, raw]) => {
          const row = object(raw)
          return <div key={system}><strong>{systemLabels[system] ?? system}</strong><dl className="resource-facts">
            <div><dt>案例 / 失败</dt><dd>{count(row.case_count)} / {count(row.failed_cases)}</dd></div>
            <div><dt>平均轨迹误差</dt><dd>{measured(row.mean_trajectory_error)}</dd></div>
            <div><dt>有效案例采样点平均能量漂移</dt><dd>{measured(row.mean_sampled_energy_drift_successful)}</dd></div>
          </dl></div>
        })}
        <details><summary>{name}逐案例记录</summary>{Array.isArray(result.cases) ? result.cases.map((raw, index) => {
          const row = object(raw)
          return <dl className="resource-facts" key={index}>
            <div><dt>案例</dt><dd>{typeof row.id === 'string' ? row.id : typeof row.case_id === 'string' ? row.case_id : `案例 ${index + 1}`} · {systemLabels[String(row.system)] ?? String(row.system ?? '系统未提供')}</dd></div>
            <div><dt>结果</dt><dd>{row.failed === true ? '案例失败' : row.failed === false ? '已登记有效轨迹' : '未提供'}</dd></div>
            <div><dt>轨迹误差 / 采样点能量漂移</dt><dd>{measured(row.trajectory_error)} / {measured(row.energy_drift)}</dd></div>
            {row.query_count != null && <div><dt>可信 RHS 查询 / 上限</dt><dd>{count(row.query_count)} / {count(row.query_budget)}</dd></div>}
          </dl>
        }) : <p className="muted">逐案例记录未提供。</p>}</details>
      </div>)}
    </details>
    <details className="resource-technical"><summary>诊断来源与对象标识</summary><dl className="resource-facts">
      {['study_id', 'job_id', 'revision_id', 'run_id', 'evidence_sha256'].map((key) => typeof evidence[key] === 'string' && <div key={key}><dt>{{ study_id: 'Study', job_id: 'Job', revision_id: 'Revision', run_id: 'Run', evidence_sha256: '证据摘要' }[key]}</dt><dd><code>{String(evidence[key])}</code></dd></div>)}
      {Object.entries(object(evidence.source_digests)).map(([key, digest]) => typeof digest === 'string' && <div key={key}><dt>{key}</dt><dd><code>{digest}</code></dd></div>)}
      <div><dt>绝对改善 / 预注册阈值</dt><dd>{measured(comparison.absolute_improvement)} / {measured(comparison.threshold)}</dd></div>
    </dl></details>
  </section>
}
