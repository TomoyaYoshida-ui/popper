import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TaskContextCard } from '../src/components/TaskContextCard'
import type { ContextPreview, HandoffFields, TaskContext } from '../src/taskContext'

const fake = vi.hoisted(() => ({ read: vi.fn(), preview: vi.fn(), commit: vi.fn() }))
vi.mock('../src/api', () => ({ fetchTaskContext: fake.read, previewTaskContext: fake.preview, commitTaskContext: fake.commit }))
const fields: HandoffFields = { goal: '', constraints: '', decisions: '', todo: '', blockers: '', next_step: '' }
const data: TaskContext = { workspace_id: 'A', tasks: [], sessions: [{ session_id: 'old', title: '旧研究讨论', updated: 'v1', status: 'active' }], binding: { version: 0, task_id: null, revision: null }, selected: null, facts: [] }
const candidate: ContextPreview = { task_id: null, title: '旧研究讨论', expected_task_version: 0, expected_binding_version: 0, preview_sha256: 'digest', fields, sources: [{ session_id: 'old', turn_id: 'turn1', version: 'v1', kind: 'dialogue_excerpt', user_excerpt: '研究 ODE', agent_suggestion: '建议已确认成功（尚未核验）', status: 'completed', truncated: false }], facts: [], study_ids: [], quality: 'source_excerpts' }
const props = { workspaceId: 'A', sessionId: 'new', projectName: '项目 A', busy: false, versionSignal: 'v1', onClose: vi.fn(), onOpenResource: vi.fn() }
beforeEach(() => { vi.clearAllMocks(); fake.read.mockResolvedValue(data); fake.preview.mockResolvedValue(candidate); fake.commit.mockResolvedValue({}) })

describe('traceable task handoff', () => {
  it('does not adopt or call a model when a new session opens', async () => {
    render(<TaskContextCard {...props} />)
    await screen.findByRole('button', { name: '继续此任务' })
    expect(fake.commit).not.toHaveBeenCalled(); expect(fake.preview).not.toHaveBeenCalled()
    expect(screen.getByText(/旧聊天不自动复制/)).toBeTruthy()
  })
  it('reviews candidate excerpts before confirming and deduplicates synchronous submit', async () => {
    fake.commit.mockReturnValue(new Promise(() => undefined))
    render(<TaskContextCard {...props} />)
    fireEvent.click(await screen.findByRole('button', { name: '继续此任务' }))
    const goal = await screen.findByLabelText('任务目标')
    expect((goal as HTMLTextAreaElement).value).toBe('研究 ODE')
    expect((screen.getByLabelText('已确认的决定') as HTMLTextAreaElement).value).toBe('')
    fireEvent.change(screen.getByLabelText('下一步'), { target: { value: '先核对真实 Claim' } })
    fireEvent.click(screen.getByRole('button', { name: '确认接续（不发送）' }))
    fireEvent.submit(goal.closest('form')!)
    expect(fake.commit).toHaveBeenCalledOnce()
    expect(fake.commit.mock.calls[0][0]).toBe('A'); expect(fake.commit.mock.calls[0][1]).toBe('new')
    expect(fake.commit.mock.calls[0][2].fields.next_step).toBe('先核对真实 Claim')
    expect(fake.commit.mock.calls[0][2].preview).toEqual(candidate)
  })
  it('cancels without writing and starts new without inheriting intent', async () => {
    render(<TaskContextCard {...props} />)
    fireEvent.click(await screen.findByRole('button', { name: '继续此任务' }))
    fireEvent.click(await screen.findByRole('button', { name: '取消交接' }))
    expect(fake.commit).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '开始新任务', exact: true }))
    await waitFor(() => expect(fake.commit).toHaveBeenCalledOnce())
    expect(fake.commit.mock.calls[0][2].op).toBe('detach')
  })
  it('preserves edited fields on conflict and recheck', async () => {
    fake.commit.mockRejectedValue(new Error('来源版本已变化，请重新核对'))
    render(<TaskContextCard {...props} />)
    fireEvent.click(await screen.findByRole('button', { name: '继续此任务' }))
    fireEvent.change(await screen.findByLabelText('任务目标'), { target: { value: '保留人工修改目标' } })
    fireEvent.click(screen.getByRole('button', { name: '确认接续（不发送）' }))
    await screen.findByRole('alert')
    expect((screen.getByLabelText('任务目标') as HTMLTextAreaElement).value).toBe('保留人工修改目标')
    fireEvent.click(screen.getByRole('button', { name: '重新核对来源（保留填写）' }))
    await waitFor(() => expect(fake.preview).toHaveBeenCalledTimes(2))
    expect((screen.getByLabelText('任务目标') as HTMLTextAreaElement).value).toBe('保留人工修改目标')
  })
  it('ignores a late preview after context version changes', async () => {
    let finish!: (value: ContextPreview) => void
    fake.preview.mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const view = render(<TaskContextCard {...props} />)
    fireEvent.click(await screen.findByRole('button', { name: '继续此任务' }))
    view.rerender(<TaskContextCard {...props} versionSignal="v2" />)
    await act(async () => { finish(candidate) })
    expect(screen.queryByLabelText('任务目标')).toBeNull()
  })
  it('does not clear confirmed handoff on refresh and keeps failure local', async () => {
    fake.read.mockRejectedValueOnce(new Error('交接读取失败'))
    render(<TaskContextCard {...props} />)
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }))
    await screen.findByRole('button', { name: '继续此任务' })
    expect(fake.commit).not.toHaveBeenCalled()
  })
  it('reconciles a timeout with the same receipt and preserves edited fields', async () => {
    fake.commit.mockRejectedValueOnce(new Error('交接请求超时，保存结果可能已生效'))
    render(<TaskContextCard {...props} />)
    fireEvent.click(await screen.findByRole('button', { name: '继续此任务' }))
    fireEvent.change(await screen.findByLabelText('任务目标'), { target: { value: '超时后仍保留此人工目标' } })
    fireEvent.click(screen.getByRole('button', { name: '确认接续（不发送）' }))
    await screen.findByRole('alert')
    expect((screen.getByLabelText('任务目标') as HTMLTextAreaElement).value).toBe('超时后仍保留此人工目标')
    fireEvent.click(screen.getByRole('button', { name: '确认接续（不发送）' }))
    await waitFor(() => expect(fake.commit).toHaveBeenCalledTimes(2))
    expect(fake.commit.mock.calls[1][2]).toEqual(fake.commit.mock.calls[0][2])
  })
})
