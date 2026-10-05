import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AgentChatPane } from '../src/components/AgentChatPane'
import type { ChatState } from '../src/types'
import { StrictMode } from 'react'
const chat = { configured: true, model: 'test-model', sessions: [{ session_id: 'S1', title: 'Research' }], session_id: 'S1',
  usage: { total_tokens: 0, token_budget: null, currency: null }, turns: [], tool_calls: [], messages: [], labels: {} } as unknown as ChatState
function props(onSend = vi.fn(async () => true)) {
  return { chat, chatPending: false, contextKey: 'P:S1', projectName: 'Project', studyLabel: null, review: null, onSend,
    onToolAction: vi.fn(async () => undefined), onContinueTools: vi.fn(async () => undefined), onCancelTurn: vi.fn(async () => undefined),
    onRetryTurn: vi.fn(async () => undefined), onSteerTurn: vi.fn(async () => true), onSetBudget: vi.fn(async () => undefined),
    onSetCostBudget: vi.fn(async () => undefined), onAdjudicate: vi.fn(), onOpenResource: vi.fn() }
}
describe('Agent composer', () => {
  const completedChat = { ...chat, turns: [{ turn_id: 'T1', status: 'completed', user_message: '研究方案', assistant_message: '读取结果', assistant_draft: '' }],
    tool_calls: [{ call_id: 'C1', turn_id: 'T1', status: 'invalid', tool_name: 'bad-tool', arguments: {}, risk: 'read', executed: false,
      error: { type: 'invalid_arguments', message: '参数错误' } }],
  } as unknown as ChatState
  it('shows a server continuation block without changing the tool status or clearing the draft', () => {
    const p = props()
    const view = render(<AgentChatPane {...p} chat={completedChat} />)
    const input = screen.getByRole('textbox', { name: '向 Agent 发送研究消息' })
    fireEvent.change(input, { target: { value: '保留当前研究问题' } })
    view.rerender(<AgentChatPane {...p} chat={{ ...completedChat, continuation: {
      state: 'blocked', reason: '共享预算不足，未发出下一次模型请求', source_turn_id: 'T1', correction_attempts: 2, max_corrections: 2,
    } }} />)
    expect(screen.getByText('续推理暂不可继续')).toBeTruthy()
    expect(screen.getByText('共享预算不足，未发出下一次模型请求')).toBeTruthy()
    expect(screen.getByText('参数纠错：2 / 2 次')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '根据真实工具结果继续 →' })).toBeNull()
    expect(screen.getByText('参数无效，未执行')).toBeTruthy()
    expect((input as HTMLTextAreaElement).value).toBe('保留当前研究问题')
    expect(localStorage.getItem('popper-composer:P:S1')).toBe('保留当前研究问题')
  })
  it('waits for approval instead of offering a continuation action', () => {
    render(<AgentChatPane {...props()} chat={{ ...completedChat, continuation: { state: 'waiting_approval', source_turn_id: 'T1' } }} />)
    expect(screen.getByText('等待工具审批')).toBeTruthy()
    expect(screen.getByText(/此提示不会替代任何创建或执行审批/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: '根据真实工具结果继续 →' })).toBeNull()
  })
  it('waits for a real research result without offering continuation or clearing the draft', () => {
    const p = props()
    const waitingChat = { ...completedChat, continuation: { state: 'waiting_job' as const, source_turn_id: 'T1' },
      tool_calls: [{ ...completedChat.tool_calls[0], status: 'completed', tool_name: 'research.revision_run',
        error: null, result: { job: { job_id: 'JOBR-test', status: 'queued' } } }],
    } as unknown as ChatState
    const view = render(<AgentChatPane {...p} chat={waitingChat} />)
    const input = screen.getByRole('textbox', { name: '向 Agent 发送研究消息' })
    fireEvent.change(input, { target: { value: '等待时保留研究问题' } })
    expect(screen.getByText('等待研究结果')).toBeTruthy()
    expect(screen.getByText(/已入队不代表实验完成/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: '根据真实工具结果继续 →' })).toBeNull()
    expect(p.onContinueTools).not.toHaveBeenCalled()
    view.rerender(<AgentChatPane {...p} chat={{ ...waitingChat, continuation: {
      state: 'waiting_job', reason: '候选开发 Job 仍在运行，未形成 Observation', source_turn_id: 'T1',
    } }} />)
    expect(screen.getByText('候选开发 Job 仍在运行，未形成 Observation')).toBeTruthy()
    expect((input as HTMLTextAreaElement).value).toBe('等待时保留研究问题')
    expect(localStorage.getItem('popper-composer:P:S1')).toBe('等待时保留研究问题')
    view.rerender(<AgentChatPane {...p} chat={{ ...waitingChat, continuation: {
      state: 'ready', source_turn_id: 'T1',
    } }} />)
    expect(screen.getByRole('button', { name: '根据真实工具结果继续 →' })).toBeTruthy()
    expect((input as HTMLTextAreaElement).value).toBe('等待时保留研究问题')
  })
  it('continues only the explicit source turn and preserves input when ready', () => {
    const p = props()
    render(<AgentChatPane {...p} chat={{ ...completedChat, turns: [...completedChat.turns,
      { ...completedChat.turns[0], turn_id: 'T2', assistant_message: '另一轮' }],
      continuation: { state: 'ready', source_turn_id: 'T1' } }} />)
    const input = screen.getByRole('textbox', { name: '向 Agent 发送研究消息' })
    fireEvent.change(input, { target: { value: '未发送草稿' } })
    fireEvent.click(screen.getByRole('button', { name: '根据真实工具结果继续 →' }))
    expect(p.onContinueTools).toHaveBeenCalledWith('T1')
    expect((input as HTMLTextAreaElement).value).toBe('未发送草稿')
  })
  it('does not guess a source turn for an unavailable continuation reference', () => {
    render(<AgentChatPane {...props()} chat={{ ...completedChat, continuation: { state: 'ready', source_turn_id: 'missing-turn' } }} />)
    expect(screen.queryByRole('button', { name: '根据真实工具结果继续 →' })).toBeNull()
  })
  it('retains legacy continuation for invalid terminal calls when the new projection is absent', () => {
    const p = props()
    render(<AgentChatPane {...p} chat={completedChat} />)
    fireEvent.click(screen.getByRole('button', { name: '根据真实工具结果继续 →' }))
    expect(p.onContinueTools).toHaveBeenCalledWith('T1')
  })
  it('retains failed project adjudication reasons and distinguishes them from Study approval', async () => {
    const p = props()
    render(<AgentChatPane {...p} review={{ rejection_side: { risks: { R1: { risk_id: 'R1', status: 'fail', reason: '合成风险', user_adjudication: true } }, summary: { status: 'fail', counts: {} } }, fraud_side: { modes: [], summary: { count: 0, hits: [] } } }}
      onAdjudicate={vi.fn(async () => { throw new Error('项目裁决请求失败') })} />)
    fireEvent.click(screen.getByRole('button', { name: '处置这项风险…' }))
    const reason = screen.getByPlaceholderText('留痕理由（必填，入事件流）')
    fireEvent.change(reason, { target: { value: '保留这段核对理由' } })
    fireEvent.click(screen.getByRole('button', { name: '放行（留痕）' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('项目裁决请求失败'))
    expect((reason as HTMLInputElement).value).toBe('保留这段核对理由')
    expect(screen.getByText('项目评审待裁决 · Project')).toBeTruthy()
  })
  it('does not erase the next message when an earlier send resolves late', async () => {
    let complete!: (ok: boolean) => void
    const p = props(vi.fn(() => new Promise<boolean>((resolve) => { complete = resolve })))
    render(<AgentChatPane {...p} />)
    const input = screen.getByRole('textbox', { name: '向 Agent 发送研究消息' })
    fireEvent.change(input, { target: { value: 'first message' } })
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    fireEvent.change(input, { target: { value: 'next message' } })
    complete(true)
    await waitFor(() => expect((input as HTMLTextAreaElement).value).toBe('next message'))
    expect(localStorage.getItem('popper-composer:P:S1')).toBe('next message')
  })
  it('restores the persisted composer under StrictMode without erasing it', async () => {
    localStorage.setItem('popper-composer:P:S1', '恢复的研究问题')
    render(<StrictMode><AgentChatPane {...props()} /></StrictMode>)
    await waitFor(() => expect((screen.getByRole('textbox', { name: '向 Agent 发送研究消息' }) as HTMLTextAreaElement).value).toBe('恢复的研究问题'))
    expect(localStorage.getItem('popper-composer:P:S1')).toBe('恢复的研究问题')
  })
  it('respects IME composition and Shift+Enter before submitting Enter', async () => {
    const p = props(); render(<AgentChatPane {...p} />)
    const input = screen.getByRole('textbox', { name: '向 Agent 发送研究消息' })
    fireEvent.change(input, { target: { value: '研究问题' } })
    fireEvent.compositionStart(input); fireEvent.keyDown(input, { key: 'Enter' })
    expect(p.onSend).not.toHaveBeenCalled()
    fireEvent.compositionEnd(input); fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(p.onSend).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(p.onSend).toHaveBeenCalledWith('研究问题'))
    await waitFor(() => expect((input as HTMLTextAreaElement).value).toBe(''))
  })
  it('keeps drafts isolated by session and retains failed sends', async () => {
    const p = props(vi.fn(async () => false)); const view = render(<AgentChatPane {...p} />)
    fireEvent.change(screen.getByRole('textbox', { name: '向 Agent 发送研究消息' }), { target: { value: 'S1 draft' } })
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    await waitFor(() => expect(localStorage.getItem('popper-composer:P:S1')).toBe('S1 draft'))
    view.rerender(<AgentChatPane {...p} contextKey="P:S2" />)
    expect((screen.getByRole('textbox', { name: '向 Agent 发送研究消息' }) as HTMLTextAreaElement).value).toBe('')
    view.rerender(<AgentChatPane {...p} />)
    await waitFor(() => expect((screen.getByRole('textbox', { name: '向 Agent 发送研究消息' }) as HTMLTextAreaElement).value).toBe('S1 draft'))
  })
})
