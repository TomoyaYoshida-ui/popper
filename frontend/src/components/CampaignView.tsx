import { useState } from 'react'
import { postCampaignDecision } from '../api'
import type { Campaign, CampaignApproval } from '../api'

export function CampaignView({ campaign, reload, stageStatusLabels }: {
  campaign: Campaign | null
  reload: () => void
  stageStatusLabels: Record<string, string>
}) {
  const [sel, setSel] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [reason, setReason] = useState('')

  const decide = async (action: 'approve' | 'reject') => {
    setBusy(action)
    setMsg(null)
    try {
      const r = await postCampaignDecision(action, reason.trim())
      setMsg(action === 'approve'
        ? (r.decision === 'approved' ? '已批准，后台开始恢复执行…' : '已批准')
        : '已驳回，campaign 保持挂起')
      reload()
    } catch (e) {
      setMsg((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  if (!campaign) return <div className="notice">正在读取统一切磋进度…</div>
  const data = campaign

  const selected = data.stages.find((s) => s.key === sel) ?? null
  const ap: CampaignApproval | null = data.campaign
  const jobRunning = ap?.job?.status === 'completed'

  return (
    <>
      <p className="eyebrow">CAMPAIGN / 统一进度</p>
      <h1>{data.objective ?? '实验推进'}</h1>
      <p className="muted">
        阶段流水线（Idea → 文献 → 假设环 → Popper 开发 → 冻结 → 确认 → 物化）· 当前阶段{' '}
        <span className="ok">{data.phase_label}</span> · 全部取自真实状态
      </p>

      {ap && (
        <div className="section box" style={{ borderColor: 'rgba(255,159,10,.5)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
            <span className="schip warn">待人工审批</span>
            <h2 style={{ fontSize: 15, margin: 0 }}>步骤「{ap.step}」挂起</h2>
            <span className="muted" style={{ marginLeft: 'auto', fontSize: 11 }}>{ap.run_dir}</span>
          </div>
          <p className="muted" style={{ marginTop: 0 }}>{ap.reason ?? '步骤需要人工审批后恢复。'}</p>

          <div className="kv-list">
            <div className="kv"><span className="k">审批标记</span><span className="v"><code>{ap.required}</code></span></div>
            <div className="kv"><span className="k">运行目录</span><span className="v">{ap.run_dir}</span></div>
          </div>

          {ap.evidence?.diff && (
            <details style={{ margin: '12px 0' }}>
              <summary className="muted" style={{ cursor: 'pointer', fontSize: 12 }}>proposal.diff 预览（前 4000 字符）</summary>
              <pre style={{ background: '#101014', border: '1px solid var(--border)', padding: 12, fontSize: 11,
                           overflow: 'auto', maxHeight: 260, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{ap.evidence.diff}</pre>
            </details>
          )}
          {ap.evidence?.proposal && (
            <p className="muted" style={{ fontSize: 11, marginTop: 4 }}>
              proposal.json · {ap.evidence.proposal.bytes} 字节
              {jobRunning && <span className="ok"> · 已批准并恢复完成</span>}
            </p>
          )}

          <div style={{ display: 'flex', gap: 10, marginTop: 14, flexWrap: 'wrap' }}>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="审批备注（可选）"
              style={{ flex: 1, minWidth: 200, background: 'var(--panel)', color: 'var(--fg)',
                       border: '1px solid var(--border)', borderRadius: 8, padding: '8px 10px' }}
            />
            <button className="btn primary" disabled={!!busy || jobRunning}
              onClick={() => decide('approve')}>
              {busy === 'approve' ? '恢复中…' : '批准并恢复'}
            </button>
            <button className="btn ghost" disabled={!!busy || jobRunning}
              style={{ color: 'var(--red)' }} onClick={() => decide('reject')}>
              {busy === 'reject' ? '处理中…' : '驳回'}
            </button>
          </div>

          {msg && <p className={msg.includes('失败') || msg.includes('trusted') || msg.includes('--trusted-local') ? 'err' : 'ok'}
            style={{ fontSize: 12, marginTop: 10 }}>{msg}</p>}
          {ap.job?.status === 'failed' && ap.job.error && (
            <p className="err" style={{ fontSize: 12, marginTop: 10 }}>恢复失败：{ap.job.error}</p>
          )}
        </div>
      )}

      <div className="camp-track">
        {data.stages.map((s, i) => (
          <button key={s.key} className={`cam-node ${s.status} ${selected?.key === s.key ? 'sel' : ''}`}
            onClick={() => setSel(s.key)}>
            <span className="idx">{i + 1}</span>
            <span className="lb">{s.label}</span>
            <span className="st">{stageStatusLabels[s.status] ?? ''}</span>
          </button>
        ))}
      </div>

      <div className="section box">
        {selected ? (
          <div>
            <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
              <h2 style={{ fontSize: 15 }}>{selected.label}</h2>
              <span className={`schip ${selected.status === 'done' ? 'pass' : selected.status === 'warn' ? 'warn' : 'open'}`}>
                {stageStatusLabels[selected.status] ?? ''}
              </span>
              <span className="muted" style={{ marginLeft: 'auto', fontSize: 11 }}>数据源 · {selected.src}</span>
            </div>
            <div className="kv-list">
              {selected.evidence.map((e, i) => (
                <div className="kv" key={i}>
                  <span className="k">{e.k}</span>
                  <span className="v">{e.v}</span>
                </div>
              ))}
            </div>
            <p className="muted" style={{ fontSize: 11, marginTop: 8 }}>
              证据全部来自 {selected.src} 的真实登记产物；状态按后端现状自动判定，无假数据。
            </p>
          </div>
        ) : (
          <p className="muted">点击上方任一阶段查看真实证据摘要。</p>
        )}
      </div>
    </>
  )
}