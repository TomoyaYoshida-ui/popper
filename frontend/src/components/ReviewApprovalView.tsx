import { useState } from 'react'
import { adjudicate } from '../api'
import type { PopperState, RiskEntry } from '../types'

export function ReviewApprovalView({ data }: { data: PopperState }) {
  const state = data.state as { phase: string; claim: { claim_id: string; status: string; delta: number; threshold: number; scope: string; unit: string } | null }
  const review = data.review

  const risks: Record<string, RiskEntry> = review?.rejection_side.risks ?? {}
  const summary = review?.rejection_side.summary
  const fraud = review?.fraud_side.summary
  const adjudications = review?.adjudications ?? {}
  const [reason, setReason] = useState<Record<string, string>>({})

  // 需用户裁决（机器产证据、用户裁决）：R6/R7
  const adjudication = Object.values(risks).filter((r) => r.user_adjudication)
  // provisional（永不二值）：R1
  const provisional = Object.values(risks).filter((r) => r.provisional)
  // 机械致命（拒稿侧 fail，非用户裁决项）
  const mechanicalFails = Object.values(risks).filter((r) => r.status === 'fail' && !r.user_adjudication)

  const claim = state.claim

  return (
    <>
      <p className="eyebrow">HUMAN REVIEW / 审批</p>
      <h1>审批中心</h1>
      <p className="muted">机器产证据、用户裁决。novelty 分级永不二值；R6/R7 需人工判断。裁定全程留痕入事件源。</p>

      <div className="section box">
        <h2 style={{ fontSize: 16 }}>结论判定</h2>
        {claim ? (
          <>
            <div style={{ fontSize: 28, fontFamily: 'ui-monospace,monospace', margin: '8px 0' }} className={claim.status === 'supports_threshold' ? 'ok' : ''}>
              {claim.delta.toPrecision(4)}
            </div>
            <p className="muted">
              绝对 {claim.unit || '改善'} / 阈值 {claim.threshold} ·{' '}
              {claim.status === 'supports_threshold'
                ? <span className="ok">达到预注册改善阈值</span>
                : '证据不足（不自动解释为证伪）'}
            </p>
            <p className="muted" style={{ fontSize: 12 }}>{claim.scope}</p>
          </>
        ) : (
          <p className="muted">尚未形成结论。最终确认后才生成 claim。</p>
        )}
      </div>

      <div className="section box">
        <h2 style={{ fontSize: 16 }}>需用户裁决</h2>
        {adjudication.length === 0 ? (
          <p className="muted">当前无待人工裁决项。</p>
        ) : (
          adjudication.map((r) => {
            const done = adjudications[r.risk_id] ?? null
            return (
              <div className="row" key={r.risk_id}>
                <span className="metric">{r.risk_id}</span>
                <span className={`st ${done ? (done.verdict === 'allow' ? 'pass' : 'fail') : r.status}`} />
                <span style={{ flex: 1 }}>
                  {r.reason}
                  {done && (
                    <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>
                      已裁定：{done.verdict === 'allow' ? '放行' : '驳回'} · {done.reason}
                    </div>
                  )}
                </span>
                {done ? (
                  <span className="badge ok">已处置</span>
                ) : (
                  <span className="adj-ops">
                    <input className="ginput" placeholder="留痕理由（必填）"
                      value={reason[r.risk_id] ?? ''}
                      onChange={(e) => setReason((m) => ({ ...m, [r.risk_id]: e.target.value }))} />
                    <button className="gbtn ok" disabled={!(reason[r.risk_id] ?? '').trim()}
                      onClick={() => doAdjudicate(r.risk_id, 'allow', reason[r.risk_id])}>放行</button>
                    <button className="gbtn warn" disabled={!(reason[r.risk_id] ?? '').trim()}
                      onClick={() => doAdjudicate(r.risk_id, 'reject', reason[r.risk_id])}>驳回</button>
                  </span>
                )}
              </div>
            )
          })
        )}
        {provisional.length > 0 && (
          <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            provisional（永不定二值）：{provisional.map((r) => r.risk_id).join('、')}
          </p>
        )}
      </div>

      <div className="section box">
        <h2 style={{ fontSize: 16 }}>机械致命项</h2>
        {mechanicalFails.length === 0 ? (
          <p className="muted">无机械致命项（拒稿侧 fail 且非用户裁决）。</p>
        ) : (
          mechanicalFails.map((r) => (
            <div className="row" key={r.risk_id}>
              <span className="metric">{r.risk_id}</span>
              <span className="err">{r.reason}</span>
            </div>
          ))
        )}
      </div>

      <div className="section box">
        <h2 style={{ fontSize: 16 }}>汇总</h2>
        {summary ? (
          <div className="row">
            <span className="muted">拒稿侧</span>
            <span className="metric">pass {summary.counts.pass ?? 0} / warn {summary.counts.warn ?? 0} / fail {summary.counts.fail ?? 0}</span>
          </div>
        ) : null}
        {fraud ? (
          <div className="row">
            <span className="muted">造假侧命中</span>
            <span className="metric" style={{ color: fraud.hits.length > 0 ? 'var(--red)' : 'var(--ok)' }}>
              {fraud.hits.length > 0 ? fraud.hits.join('、') : '无'}
            </span>
          </div>
        ) : null}
      </div>
    </>
  )

  /* 裁定成功后调用方立即拉取 /api/state；之后 SSE /api/events 的 state 补丁也会带出最新 adjudications。 */
  async function doAdjudicate(riskId: string, verdict: 'allow' | 'reject', reasonText?: string) {
    if (!reasonText || !reasonText.trim()) return
    try {
      await adjudicate(riskId, verdict, reasonText.trim())
    } catch (e) {
      window.alert((e as Error).message)
    }
  }
}