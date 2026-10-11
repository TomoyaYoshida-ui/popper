import type { ResearchClaim } from './api'

export function scientificClaimLabel(claim: ResearchClaim): string {
  return ({ supported: '登记预测获得独立证据支持', refuted: '登记预测被独立证据反驳',
    inconclusive: '证据不足', not_evaluated: '尚无独立科学判断' } as Record<string, string>)[claim.scientific_status ?? 'not_evaluated'] ?? '尚无独立科学判断'
}

export function scientificClaimEffect(claim: ResearchClaim): string {
  const value = claim.effect_value ?? claim.oriented_delta
  return `${claim.effect_type ?? '旧描述性绝对差'} ${Number(value).toPrecision(5)} ${claim.effect_unit ?? claim.metric}`
}
