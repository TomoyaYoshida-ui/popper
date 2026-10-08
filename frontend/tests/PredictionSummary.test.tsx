import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PredictionSummary } from '../src/components/PredictionSummary'

describe('prediction assurance', () => {
  it('keeps a threshold result separate from statistical or mechanism evidence', () => {
    render(<PredictionSummary value={{ prediction_result: { status: 'met', oriented_effect: .17,
      statistical_evidence_sufficient: false, prediction: { metric: 'macro_f1', unit: 'fraction', threshold: .05 } } }} />)
    expect(screen.getByText(/达到注册预测阈值/)).not.toBeNull()
    expect(screen.getByText(/统计证据尚未充分验证/)).not.toBeNull()
    expect(screen.getByText(/机制解释仍待/)).not.toBeNull()
  })
  it('does not upgrade historical free text', () => {
    render(<PredictionSummary value={{ predictions: ['accuracy exceeds 0.577'] }} />)
    expect(screen.getByText(/历史文字预测尚未验证/)).not.toBeNull()
  })
})
