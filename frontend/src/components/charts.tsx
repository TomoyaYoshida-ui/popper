// 通用 SVG 图表：零第三方依赖，全部由后端真实数据驱动，不造任何数。
// 配色沿用 index.css 的 CSS 变量，跟随明暗主题。

export type BarPoint = {
  label: string
  mean: number
  std?: number | null
  seeds?: { seed: number; value: number }[]
  /** 强调项（如基线），用弱色与其余区分 */
  highlight?: boolean
}

function cut(text: string, max: number): string {
  return text.length > max ? text.slice(0, max - 1) + '…' : text
}

function fmt(v: number): string {
  return Number.isFinite(v) ? v.toPrecision(6) : '—'
}

/** 水平条形图：mean ± std 误差条 + per-seed 散点 + 可选阈值参考线。 */
export function MetricBars({ points, direction, threshold, thresholdLabel, empty }: {
  points: BarPoint[]
  direction?: 'min' | 'max'
  threshold?: number | null
  thresholdLabel?: string
  empty?: string
}) {
  if (points.length === 0) {
    return empty ? <div className="notice">{empty}</div> : null
  }
  // 比例尺由全部真实数值决定：mean、误差条端点、每个种子点、阈值线。
  const values: number[] = []
  for (const p of points) {
    values.push(p.mean)
    if (p.std != null && Number.isFinite(p.std)) {
      values.push(p.mean + p.std, p.mean - p.std)
    }
    for (const s of p.seeds ?? []) values.push(s.value)
  }
  if (threshold != null && Number.isFinite(threshold)) values.push(threshold)
  const finite = values.filter((v) => Number.isFinite(v))
  if (finite.length === 0) return <div className="notice">数值不可用，无法绘图。</div>
  let lo = Math.min(...finite)
  let hi = Math.max(...finite)
  if (hi - lo < 1e-12) { hi += 1; lo -= 1 }
  const pad = (hi - lo) * 0.08
  lo -= pad
  hi += pad

  const W = 640
  const labelW = 148
  const valW = 84
  const top = 18
  const rowH = 34
  const H = top + points.length * rowH + 6
  const x = (v: number) => labelW + ((v - lo) / (hi - lo)) * (W - labelW - valW)
  const gridX = [lo, hi]

  return (
    <div>
      <div className="muted" style={{ fontSize: 11, display: 'flex', justifyContent: 'space-between' }}>
        <span>{direction === 'min' ? '↓ 更低更好' : direction === 'max' ? '↑ 更高更好' : ''}</span>
        {threshold != null && thresholdLabel && (
          <span>┄ {thresholdLabel} = {fmt(threshold)}</span>
        )}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }} role="img"
           aria-label="指标对比条形图">
        {gridX.map((v, i) => (
          <text key={i} x={x(v)} y={12} fontSize={10} fill="var(--faint)" textAnchor={i === 0 ? 'start' : 'end'}>
            {fmt(v)}
          </text>
        ))}
        {threshold != null && Number.isFinite(threshold) && (
          <line x1={x(threshold)} x2={x(threshold)} y1={top - 4} y2={H - 2}
                stroke="var(--warn)" strokeWidth={1} strokeDasharray="4 3" />
        )}
        {points.map((p, i) => {
          const y = top + i * rowH + rowH / 2
          const barTop = y - 6
          const hasStd = p.std != null && Number.isFinite(p.std)
          return (
            <g key={`${p.label}-${i}`}>
              <text x={0} y={y + 3} fontSize={10} fill="var(--muted)" fontFamily="var(--mono)">
                {cut(p.label, 24)}
              </text>
              {/* 条体：从比例尺左端到 mean；基线用弱色，候选用主题色 */}
              <rect x={x(lo)} y={barTop} width={Math.max(1, x(p.mean) - x(lo))} height={12} rx={3}
                    fill={p.highlight ? 'var(--muted)' : 'var(--accent)'} opacity={p.highlight ? 0.55 : 0.9} />
              {hasStd && (
                <g stroke="var(--fg)" opacity={0.55} strokeWidth={1}>
                  <line x1={x(p.mean - (p.std as number))} x2={x(p.mean + (p.std as number))} y1={y} y2={y} />
                  <line x1={x(p.mean - (p.std as number))} x2={x(p.mean - (p.std as number))} y1={y - 3} y2={y + 3} />
                  <line x1={x(p.mean + (p.std as number))} x2={x(p.mean + (p.std as number))} y1={y - 3} y2={y + 3} />
                </g>
              )}
              {(p.seeds ?? []).map((s) => (
                <circle key={s.seed} cx={x(s.value)} cy={y} r={2.4} fill="var(--blue)" opacity={0.85} />
              ))}
              <text x={W} y={y + 3} fontSize={10} fill="var(--fg)" textAnchor="end" fontFamily="var(--mono)">
                {fmt(p.mean)}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

/** 预算堆叠条：已花费 / 预留 / 可用，比例按真实账本数字。 */
export function BudgetBar({ cap, spent, reserved }: {
  cap: number; spent: number; reserved: number; available: number
}) {
  const W = 640
  const H = 14
  const safe = (v: number) => (Number.isFinite(v) && v > 0 ? v : 0)
  const total = safe(cap)
  if (total <= 0) return null
  const seg = (v: number) => (safe(v) / total) * W
  const spentW = seg(spent)
  const reservedW = seg(reserved)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block', margin: '4px 0' }}
         role="img" aria-label="预算堆叠条">
      <rect x={0} y={0} width={W} height={H} rx={4} fill="var(--card-2)" stroke="var(--border)" strokeWidth={1} />
      <rect x={0} y={0} width={spentW} height={H} rx={4} fill="var(--accent)" opacity={0.9} />
      {reservedW > 0 && (
        <rect x={spentW} y={0} width={reservedW} height={H} fill="var(--warn)" opacity={0.75}
              rx={reservedW >= H / 2 ? 4 : 0} />
      )}
    </svg>
  )
}

/** 图例小圆点 + 文本（BudgetBar 配套）。 */
export function LegendDot({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <span className="muted" style={{ fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      <span style={{ width: 8, height: 8, borderRadius: 2, background: color, display: 'inline-block' }} />
      {children}
    </span>
  )
}
