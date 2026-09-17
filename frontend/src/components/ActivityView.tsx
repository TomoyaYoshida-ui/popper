import type { Event } from '../types'

export function ActivityView({ events, eventNames }: { events: Event[]; eventNames: Record<string, string> }) {
  return (
    <>
      <p className="eyebrow">AUDIT / 过程可追溯</p>
      <h1>事件流</h1>
      <p className="muted">回放从保存的预测重新计算指标，不重新运行实验或调用模型。</p>

      <div className="section">
        {events.length === 0 && <p className="muted">暂无事件。</p>}
        {events.map((e) => (
          <div className="row" key={e.seq}>
            <div>
              <div><span className="code">#{String(e.seq).padStart(3, '0')}</span> · {eventNames[e.kind] ?? ''}</div>
            </div>
            <span className="muted" style={{ fontSize: 11, maxWidth: 360, overflowWrap: 'anywhere', textAlign: 'right' }}>
              {JSON.stringify(e.payload)}
            </span>
          </div>
        ))}
      </div>
    </>
  )
}