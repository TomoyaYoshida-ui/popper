import { describe, expect, it } from 'vitest'
import { currencyToMicros, microsToCurrency } from '../src/money'

describe('currency amounts', () => {
  it('converts normal money units exactly', () => {
    expect(currencyToMicros('10.50')).toBe(10_500_000)
    expect(currencyToMicros('0.000003')).toBe(3)
    expect(microsToCurrency(10_500_000)).toBe('10.5')
    expect(currencyToMicros('')).toBeNull()
  })
  it('rejects invalid, zero, and imprecise amounts', () => {
    for (const value of ['0', '-1', '1e3', '1.0000001', '999999999999999']) {
      expect(() => currencyToMicros(value)).toThrow()
    }
  })
})
