import { useEffect, useRef, useState } from 'react'
import { adjudicate } from '../api'
import type { Adjudication, PopperState, RiskEntry } from '../types'
import { useConfirmation } from './ConfirmProvider'

export function ReviewApprovalView({ data, projectName = '当前项目', workspaceId = '' }: { data: PopperState; projectName?: string; workspaceId?: string }) {
  const confirm = useConfirmation()
  const contextGeneration = useRef({ workspaceId, epoch: 0 })
  if (contextGeneration.current.workspaceId !== workspaceId) contextGeneration.current = { workspaceId, epoch: contextGeneration.current.epoch + 1 }
  const workspaceContext = `${workspaceId}:${contextGeneration.current.epoch}`
  const currentWorkspace = useRef(workspaceContext); currentWorkspace.current = workspaceContext
  const state = data.state as { phase: string; claim: { claim_id: string; status: string; delta: number; threshold: number; scope: string; unit: string } | null }
  const review = data.review

  const risks: Record<string, RiskEntry> = review?.rejection_side.risks ?? {}
  const summary = review?.rejection_side.summary
  const fraud = review?.fraud_side.summary
  const [submitted, setSubmitted] = useState<{ context: string; risks: Record<string, Adjudication> } | null>(null)
  const adjudications = { ...(submitted?.context === workspaceContext ? submitted.risks : {}), ...review?.adjudications }
  const [reason, setReason] = useState<Record<string, string>>({})
  const requestInFlight = useRef(false)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  useEffect(() => { setReason({}); setFailure(null); setBusy(requestInFlight.current) }, [workspaceId])

  // 需用户裁决（机器产证据、用户裁决）：R6/R7
  const adjudication = Object.values(risks).filter((r) => r.user_adjudication)
  // provisional（永不二值）：R1
  const provisional = Object.values(risks).filter((r) => r.provisional)
  // 机械致命（拒稿侧 fail，非用户裁决项）
  const mechanicalFails = Object.values(risks).filter((r) => r.status === 'fail' && !r.user_adjudication)

  const claim = state.claim

  return (
    <>
      <p className="eyebrow">项目评审 / HUMAN REVIEW</p>
      <h1>项目评审裁决</h1>
      <p className="muted">所属项目：{projectName}。此处处理项目旧实验的评审风险，不是当前 Study 的独立确认，也不是 Agent 工具审批。</p>
      <p className="muted">机器产证据、用户裁决。novelty 分级永不二值；R6/R7 需人工判断。裁定全程留痕入事件源。</p>
      <div className="notice">下一步：{adjudication.some(r => !adjudications[r.risk_id]) ? '审查以下待裁决项，并填写留痕理由。' : mechanicalFails.length ? '核对机械失败项的证据；人工裁定不能覆盖机器评测。' : '当前没有待裁决项，可继续核对研究证据或审校稿件。'}</div>
      {failure && <div className="chat-error" role="alert">{failure} · 输入理由已保留，可重新提交。</div>}

      <div className="section box">
        <h2 style={{ fontSize: 16 }}>旧实验结论判定</h2>
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
          <p className="muted">旧实验尚未形成结论。Study 的研究结论请从所属研究查看。</p>
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
                    <input className="ginput" placeholder="留痕理由（必填）" aria-label={`${r.risk_id} 裁决理由`} disabled={busy}
                      value={reason[r.risk_id] ?? ''}
                      onChange={(e) => setReason((m) => ({ ...m, [r.risk_id]: e.target.value }))} />
                    <button className="gbtn ok" title={!(reason[r.risk_id] ?? '').trim() ? '请先填写留痕理由' : undefined} disabled={busy || !(reason[r.risk_id] ?? '').trim()}
                      onClick={() => doAdjudicate(r.risk_id, 'allow', reason[r.risk_id])}>放行</button>
                    <button className="gbtn warn" title={!(reason[r.risk_id] ?? '').trim() ? '请先填写留痕理由' : undefined} disabled={busy || !(reason[r.risk_id] ?? '').trim()}
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

      <details className="section box">
        <summary>评审记录与技术汇总</summary>
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
      </details>
    </>
  )

  /* Show the acknowledged decision immediately; SSE remains the authoritative projection. */
  async function doAdjudicate(riskId: string, verdict: 'allow' | 'reject', reasonText?: string) {
    if (!reasonText || !reasonText.trim() || requestInFlight.current || adjudications[riskId]) return
    requestInFlight.current = true
    const requestWorkspace = workspaceContext
    setBusy(true)
    setFailure(null)
    try {
      const accepted = await confirm({ title: '登记项目评审裁决', description: '此操作只处理项目旧实验的人工评审风险，不会批准 Agent 工具或消费 Study 独立确认资源。裁决及理由将写入项目事件记录。', details: [{ label: '所属项目', value: projectName }, { label: '风险项', value: riskId }, { label: '裁决', value: verdict === 'allow' ? '放行' : '驳回' }, { label: '留痕理由', value: reasonText.trim() }], confirmLabel: '确认登记裁决', danger: verdict === 'reject' })
      if (!accepted || currentWorkspace.current !== requestWorkspace) return
      const value = await adjudicate(riskId, verdict, reasonText.trim())
      if (currentWorkspace.current === requestWorkspace) setSubmitted((previous) => ({ context: requestWorkspace, risks: { ...(previous?.context === requestWorkspace ? previous.risks : {}), [riskId]: value } }))
    } catch (e) {
      if (currentWorkspace.current === requestWorkspace) setFailure((e as Error).message)
    } finally {
      requestInFlight.current = false
      setBusy(false)
    }
  }
}
