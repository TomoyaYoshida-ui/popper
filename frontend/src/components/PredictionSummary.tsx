export function PredictionSummary({ value, judgments = [] }: { value: unknown; judgments?: Array<{ new_judgment: string; hypothesis_id: string }> }) {
  if (!value || typeof value !== 'object') return null
  const row = value as Record<string, unknown>
  const result = row.prediction_result && typeof row.prediction_result === 'object'
    ? row.prediction_result as Record<string, unknown> : null
  const prediction = result?.prediction ?? row.prediction_contract
  if (!prediction || typeof prediction !== 'object') return <p className="notice">此记录没有结构化预测契约，历史文字预测尚未验证。</p>
  const contract = prediction as Record<string, unknown>
  const states: Record<string, string> = { met: '达到注册预测阈值', not_met: '未达到注册预测阈值', inconclusive: '证据不足', not_evaluated: '尚未评估' }
  const scientific: Record<string, string> = { supported: '预测获得支持', refuted: '预测被反驳', inconclusive: '证据不足', not_evaluated: '尚未评估' }
  return <section className="box" aria-label="预测与结论边界">
    <h3>注册预测与实际验证</h3>
    {judgments.map((j) => <p key={j.hypothesis_id}>科研判断：{scientific[j.new_judgment] ?? '尚未记录'}；机制尚未证明。</p>)}
    <p>指标：{String(contract.metric)}；单位：{String(contract.unit)}；最小改善：{String(contract.threshold)}。</p>
    <p>比较候选与冻结对照，结论限定于当前注册范围。</p>
    {result && <p>{states[String(result.status)] ?? '尚未评估'}；方向统一效应：{String(result.oriented_effect ?? '未测量')}。</p>}
    <p className="muted">{result?.statistical_evidence_sufficient === true ? '已满足注册的统计判据。' : '统计证据尚未充分验证。'} 机制解释仍待区分性实验验证。</p>
  </section>
}
