import { useState } from 'react'
import type { Review } from '../types'

export function GateView({ review, riskNames, gateStatusLabels, mode7Names }: {
  review: Review | null
  riskNames: Record<string, string>
  gateStatusLabels: Record<string, string>
  mode7Names: Record<string, string>
}) {
  const [sel, setSel] = useState<string>('R1')

  if (!review) {
    return (
      <>
        <p className="eyebrow">REVIEW RISK</p>
        <h1>评审门禁 · R1-R7 拒稿树 + 7-mode</h1>
        <div className="notice">后端未返回拒稿预检数据（复现任务模式无此视图，或尚未连接）。</div>
      </>
    )
  }

  const risks = Object.entries(review.rejection_side.risks)
  const tax = Object.fromEntries(risks) as Record<string, Review['rejection_side']['risks'][string]>
  const current = review.rejection_side.risks[sel] ?? risks[0]?.[1] ?? null
  const counts = review.rejection_side.summary.counts ?? {}

  return (
    <>
      <p className="eyebrow">REVIEW RISK</p>
      <h1>评审门禁 · R1-R7 拒稿树 + 7-mode</h1>

      <div style={{ display: 'flex', gap: 12, margin: '14px 0' }}>
        <span className="badge ok">pass {counts.pass ?? 0}</span>
        <span className="badge" style={{ color: '#ff9f0a', borderColor: 'rgba(255,159,10,.3)' }}>warn {counts.warn ?? 0}</span>
        <span className="badge" style={{ color: '#ff8a8a', borderColor: 'rgba(255,107,107,.3)' }}>fail {counts.fail ?? 0}</span>
      </div>

      <div className="section box heat-wrap">
        <h2 style={{ fontSize: 16, marginBottom: 10 }}>R1-R7 拒稿热力网格</h2>
        <div className="heat" role="grid" aria-label="R1-R7 拒稿风险热力网格">
          {['R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7'].map((id) => {
            const r = tax[id]
            const state = r?.status ?? 'open'
            const flags = [
              r?.provisional ? ' provisional' : '',
              r?.user_adjudication ? ' adjud' : '',
            ].join('')
            return (
              <button key={id} role="gridcell"
                className={`heat-cell ${state}${flags} ${sel === id ? 'sel' : ''}`}
                onClick={() => setSel(id)} title={`${id} · ${riskNames[id] ?? ''}`}>
                <span className="hc-id">{id}</span>
                <span className="hc-flag">
                  {r?.user_adjudication ? '裁决' : r?.provisional ? '暂定' : gateStatusLabels[state] ?? ''}
                </span>
              </button>
            )
          })}
        </div>
        <div className="heat-legend">
          <span><i className="hdot pass" />pass</span>
          <span><i className="hdot warn" />warn</span>
          <span><i className="hdot fail" />fail</span>
          <span><i className="hdot provisional" />provisional（永不二值）</span>
          <span><i className="hdot adjud" />user_adjudication（需裁决）</span>
        </div>
      </div>

      <div className="gate-grid">
        <aside>
          {risks.map(([id, r]) => (
            <button key={id} className={`gnode ${sel === id ? 'sel' : ''}`} onClick={() => setSel(id)}>
              <span className="code">{id}</span><span style={{ flex: 1 }}>{riskNames[id] ?? ''}</span>
              <span className={`st ${r.status}`} />
            </button>
          ))}
        </aside>

        <section className="box">
          {current && (
            <>
              <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                <h2 style={{ fontSize: 16 }}>{current.risk_id} · {riskNames[current.risk_id] ?? ''}</h2>
                <span className={`st ${current.status}`} />
                <span className="muted">{gateStatusLabels[current.status] ?? ''}</span>
              </div>
              <div className="q">“{current.reason}”</div>
              <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
                {current.provisional ? '（provisional · 永不二值）' : ''}
                {current.user_adjudication ? '（机器产证据，需用户裁决）' : ''}
              </p>
            </>
          )}
        </section>
      </div>

      <div className="section">
        <h2 style={{ fontSize: 16, marginBottom: 12 }}>造假侧 · 7-mode</h2>
        <div className="modes">
          {review.fraud_side.modes.map((m) => (
            <div key={m.mode} className={`mode ${m.status === 'hit' ? 'hit' : ''}`}>
              <div><strong>{mode7Names[m.mode] ?? ''}</strong></div>
              <div className="muted" style={{ fontSize: 12 }}>
                {m.status === 'hit' ? `命中：${m.evidence ?? '证据缺失'}` : '干净'}
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  )
}
