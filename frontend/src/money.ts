export function currencyToMicros(value: string): number | null {
  if (!value.trim()) return null
  if (!/^\d+(?:\.\d{1,6})?$/.test(value.trim())) throw new Error('金额最多保留六位小数')
  const [whole, fraction = ''] = value.trim().split('.')
  const micros = Number(whole + fraction.padEnd(6, '0'))
  if (!Number.isSafeInteger(micros) || micros < 1) throw new Error('金额必须大于零且在可用范围内')
  return micros
}

export function microsToCurrency(value?: number | null): string {
  if (value == null) return ''
  return `${Math.floor(value / 1_000_000)}.${String(value % 1_000_000).padStart(6, '0')}`.replace(/0+$/, '').replace(/\.$/, '')
}

export function validCurrency(value: string): boolean { try { currencyToMicros(value); return true } catch { return false } }
