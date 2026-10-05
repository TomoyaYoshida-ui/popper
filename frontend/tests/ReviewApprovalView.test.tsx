import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ReviewApprovalView } from '../src/components/ReviewApprovalView'
import { ConfirmProvider } from '../src/components/ConfirmProvider'
import { adjudicate } from '../src/api'
import type { Adjudication, PopperState } from '../src/types'
vi.mock('../src/api', () => ({ adjudicate: vi.fn() }))
const data = { state: { phase: 'finished', claim: null }, review: { rejection_side: { risks: { R6: { risk_id: 'R6', reason: '需要人工核对范围', status: 'warn', user_adjudication: true }, R4: { risk_id: 'R4', reason: '机械证据不足', status: 'fail' } }, summary: { counts: { pass: 0, warn: 1, fail: 1 } } }, fraud_side: { summary: { hits: [] } }, adjudications: {} } } as unknown as PopperState
const panel = (workspaceId = 'project-1') => <ConfirmProvider><ReviewApprovalView data={data} projectName="合成项目" workspaceId={workspaceId} /></ConfirmProvider>
beforeEach(() => { vi.mocked(adjudicate).mockReset() })

describe('Project review is distinct from Study and Agent approvals', () => {
  it('identifies the project and never offers a human override for a mechanical failure', () => {
    render(panel())
    expect(screen.getByRole('heading', { name: '项目评审裁决' })).toBeTruthy()
    expect(screen.getByText(/所属项目：合成项目/)).toBeTruthy()
    expect(screen.getByText(/不是当前 Study 的独立确认/)).toBeTruthy()
    expect(screen.getAllByRole('button', { name: '放行' })).toHaveLength(1)
    expect(screen.getByRole('button', { name: '放行' })).toHaveProperty('disabled', true)
  })
  it('confirms the correct scope, serializes decisions and preserves the reason on failure', async () => {
    let reject!: (error: Error) => void
    const pending = new Promise<Adjudication>((_, fail) => { reject = fail })
    vi.mocked(adjudicate).mockReturnValueOnce(pending)
    render(panel())
    const input = screen.getByRole('textbox', { name: 'R6 裁决理由' })
    fireEvent.change(input, { target: { value: 'Checked the declared scope.' } })
    const allow = screen.getByRole('button', { name: '放行' })
    fireEvent.click(allow); fireEvent.click(allow)
    expect(await screen.findByRole('dialog')).toHaveProperty('textContent', expect.stringContaining('不会批准 Agent 工具或消费 Study 独立确认资源'))
    fireEvent.click(screen.getByRole('button', { name: '确认登记裁决' }))
    await waitFor(() => expect(adjudicate).toHaveBeenCalledTimes(1))
    await act(async () => { reject(new Error('Synthetic rejected write')); try { await pending } catch { /* expected */ } })
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('输入理由已保留'))
    expect(input).toHaveProperty('value', 'Checked the declared scope.')
  })
  it('acknowledges a returned decision immediately while awaiting SSE', async () => {
    vi.mocked(adjudicate).mockResolvedValue({ verdict: 'allow', reason: 'Checked source evidence.', at: 'synthetic-time' })
    render(panel())
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Checked source evidence.' } })
    fireEvent.click(screen.getByRole('button', { name: '放行' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认登记裁决' }))
    expect(await screen.findByText('已处置')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '放行' })).toBeNull()
  })
  it('drops confirmation from a previous context even when the user returns to the same project', async () => {
    const { rerender } = render(panel())
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Old context reason' } })
    fireEvent.click(screen.getByRole('button', { name: '放行' }))
    await screen.findByRole('dialog')
    rerender(panel('project-2')); rerender(panel())
    fireEvent.click(screen.getByRole('button', { name: '确认登记裁决' }))
    await act(async () => { await Promise.resolve() })
    expect(adjudicate).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox')).toHaveProperty('disabled', false)
  })
})
