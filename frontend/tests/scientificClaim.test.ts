import { describe, expect, it } from 'vitest'
import { claimCardSummary, scientificClaimLabel, transportRuleText } from '../src/scientificClaim'
import type { ResearchClaim, ResearchRunState } from '../src/api'

describe('independent scientific conclusions', () => {
  it('does not promote an absolute legacy threshold into statistical support', () => {
    const claim = { status: 'supported_in_scope', scientific_status: 'inconclusive' } as ResearchClaim
    expect(scientificClaimLabel(claim)).toBe('证据不足')
  })
  it('shows missing judgments as unevaluated', () => {
    expect(scientificClaimLabel({ status: 'supported_in_scope' } as ResearchClaim)).toBe('尚无独立科学判断。这不建立机制。')
  })
  it('names the declared transport columns and keeps the claim label from the card', () => {
    expect(transportRuleText({
      column: 'batch', search: { operator: 'le', threshold: 10 }, confirmation: { operator: 'gt', threshold: 10 },
    })).toBe('搜索 batch ≤ 10，确认 batch > 10')
    expect(transportRuleText(null)).toBeNull()
    const summary = claimCardSummary({
      claims: [],
      claim_card: {
        feature: 'ratio:part/whole', label: 'part/whole', direction: 'negative', ci95: [-0.2, 0.3],
        scientific_status: 'inconclusive', n_rows: 12,
        population_transport: {
          column: 'batch', search: { operator: 'le', threshold: 10 }, confirmation: { operator: 'gt', threshold: 10 },
        },
      },
    } as ResearchRunState)
    expect(summary?.transportText).toBe('搜索 batch ≤ 10，确认 batch > 10')
    expect(summary?.label).toBe('part/whole')
    const bare = claimCardSummary({ claims: [], claim_card: { feature: 'dose', direction: 'positive' } } as ResearchRunState)
    expect(bare?.label).toBe('dose')
    expect(bare?.transportText).toBeNull()
  })
})
