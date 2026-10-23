import type { ResearchClaim, ResearchRunState } from './api'

export type PopulationTransport = {
  column?: string | null
  search?: { operator?: string; threshold?: number } | null
  confirmation?: { operator?: string; threshold?: number } | null
  rows_discarded?: number | null
}

export type ClaimCardSummary = {
  feature: string | null
  label: string | null
  direction: 'positive' | 'negative' | null
  ci95: number[] | null
  scientific_status: ResearchClaim['scientific_status'] | null
  sentence?: string
  n_rows: number | null
  transportText?: string | null
}

export function transportRuleText(rule: PopulationTransport | null | undefined): string | null {
  const search = rule?.search
  const confirmation = rule?.confirmation
  if (!rule?.column || !search || !confirmation) return null
  if (search.operator !== 'le' || confirmation.operator !== 'gt') return null
  if (Number(search.threshold) !== Number(confirmation.threshold)) return null
  return `搜索 ${rule.column} ≤ ${search.threshold}，确认 ${rule.column} > ${confirmation.threshold}`
}

export function claimCardSummary(run: Pick<ResearchRunState, 'claims' | 'claim_card'>): ClaimCardSummary | null {
  const card = run.claim_card ?? null
  const claim = (run.claims ?? []).find((item) => item.conclusion_basis === 'locked_directional_claim' || item.claim_feature)
  if (!card && !claim) return null
  const fromClaim = Boolean(claim)
  return {
    feature: claim?.claim_feature ?? card?.feature ?? null,
    label: claim?.claim_label ?? card?.label ?? claim?.claim_feature ?? card?.feature ?? null,
    direction: claim?.claim_direction ?? card?.direction ?? null,
    ci95: fromClaim ? (claim?.ci95 ?? null) : (card?.ci95 ?? null),
    scientific_status: fromClaim ? (claim?.scientific_status ?? 'inconclusive') : (card?.scientific_status ?? null),
    sentence: claim?.compiled_sentence,
    n_rows: card?.n_rows ?? null,
    transportText: transportRuleText(card?.population_transport),
  }
}

export function scientificClaimLabel(claim: ResearchClaim): string {
  if (claim.compiled_sentence) return claim.compiled_sentence
  if ((claim.scientific_status ?? 'not_evaluated') === 'not_evaluated') return '尚无独立科学判断。这不建立机制。'
  return ({ supported: '登记预测获得独立证据支持', refuted: '登记预测被独立证据反驳',
    inconclusive: '证据不足' } as Record<string, string>)[claim.scientific_status ?? ''] ?? '尚无独立科学判断。这不建立机制。'
}

export function hypothesisStatusText(status: string, scientificStatus: string | undefined, fallback: string): string {
  if ((scientificStatus ?? 'not_evaluated') === 'not_evaluated'
      && (status === 'supported_in_scope' || status === 'contradicted_in_scope')) {
    return '开发记录，尚无独立科学判断'
  }
  return fallback
}

export function scientificClaimEffect(claim: ResearchClaim): string {
  const value = claim.effect_value ?? claim.oriented_delta
  return `${claim.effect_type ?? '旧描述性绝对差'} ${Number(value).toPrecision(5)} ${claim.effect_unit ?? claim.metric}`
}
