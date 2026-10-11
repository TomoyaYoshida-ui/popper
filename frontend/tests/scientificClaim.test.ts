import { describe, expect, it } from 'vitest'
import { scientificClaimLabel } from '../src/scientificClaim'
import type { ResearchClaim } from '../src/api'

describe('independent scientific conclusions', () => {
  it('does not promote an absolute legacy threshold into statistical support', () => {
    const claim = { status: 'supported_in_scope', scientific_status: 'inconclusive' } as ResearchClaim
    expect(scientificClaimLabel(claim)).toBe('证据不足')
  })
  it('shows missing judgments as unevaluated', () => {
    expect(scientificClaimLabel({ status: 'supported_in_scope' } as ResearchClaim)).toBe('尚无独立科学判断')
  })
})
