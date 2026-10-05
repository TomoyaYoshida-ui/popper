import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ToolCallCard } from '../src/components/ToolCallCard'
import type { ChatState } from '../src/types'
const call = { call_id: 'tool-1', tool_name: 'research.study_action', status: 'pending_approval',
  arguments: { study_id: 'study-1' }, preview: { ok: true, version_matches: true,
    study_action: { action: 'confirm', scope: 'synthetic', confirmation_cost: 1 } },
  result: { job: { job_id: 'job-1', request: { study_id: 'study-1' } } } } as unknown as ChatState['tool_calls'][number]
const diagnostics = {
  schema_version: '1.0', scope: 'dev', study_id: 'study-1', state_version: 45, job_id: 'job-1', revision_id: 'revision-1', run_id: 'run-1',
  metric: { name: 'trajectory_error', direction: 'min' }, evidence_sha256: 'evidence-hash', source_digests: { trajectory: 'trajectory-hash' },
  candidate: { observation_id: 'candidate-observation', trajectory_error: 62885, execution_status: 'succeeded', execution_cases: 32, trajectory_valid_cases: 32,
    trusted_rhs_queries: 327680, systems: { harmonic: { case_count: 16, failed_cases: 0, mean_trajectory_error: 100, mean_sampled_energy_drift_successful: 2 } },
    cases: [{ case_id: 'dev-1', system: 'harmonic', failed: false, trajectory_error: 100, energy_drift: 2, query_count: 4096, query_budget: 4096 }] },
  control: { observation_id: 'control-observation', trajectory_error: 0.15025162534812272, execution_status: 'succeeded', execution_cases: 32, trajectory_valid_cases: 32 },
  comparison: { absolute_improvement: -62884.84974837465, relative_improvement: -418530, threshold: 0.030050325069624545, development_threshold_met: false },
  current_budget: { candidate_development: { remaining: 2 } },
}
describe('Tool approvals and object links', () => {
  it('shows an invalid proposal as unexecuted without approval or unvalidated object links', () => {
    const action = vi.fn()
    const open = vi.fn()
    const invalid = { ...call, status: 'invalid', executed: false,
      original_tool_name: 'research.revision_create', tool_name: 'research.revision_create',
      raw_arguments: '{"study_id":"unverified","approval_sha256":"not-a-digest"}',
      validation_error: { code: 'invalid_arguments', message: '审批摘要必须为 SHA-256' }, error: null,
    } as ChatState['tool_calls'][number]
    render(<ToolCallCard call={invalid} projectName="合成项目" chatPending={false} onToolAction={action} onOpenResource={open} />)
    expect(screen.getByText('参数无效，未执行')).toBeTruthy()
    expect(screen.getByText(/参数校验未通过，工具没有执行/)).toBeTruthy()
    expect(screen.getByText(/审批摘要必须为 SHA-256/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /批准|继续执行|拒绝|取消|↗/ })).toBeNull()
    expect(action).not.toHaveBeenCalled()
    expect(open).not.toHaveBeenCalled()
  })
  it('keeps an unknown invalid tool name readable and treats parameter text as data', () => {
    render(<ToolCallCard call={{ ...call, status: 'invalid', tool_name: 'unknown.operation',
      arguments: {}, raw_arguments: '<script>approve()</script>', error: { type: 'unknown_tool', message: '工具未注册' } }}
      chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect(screen.getByText('unknown.operation')).toBeTruthy()
    expect(screen.getByText(/工具未注册/)).toBeTruthy()
    expect(screen.getByText('"<script>approve()</script>"')).toBeTruthy()
    expect(document.querySelector('script')).toBeNull()
  })
  it('reads the bounded raw JSON and original name from the validation receipt', () => {
    render(<ToolCallCard call={{ ...call, status: 'invalid', tool_name: 'research.revision_create', arguments: {}, error: null,
      validation_error: { type: 'invalid_json', message: '参数不是有效 JSON',
        original_tool_name: 'research_revision_create', raw_arguments: '{broken-json', executed: false } }}
      chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect(screen.getByText('原工具名：research_revision_create')).toBeTruthy()
    expect(screen.getByText('"{broken-json"')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /批准|执行/ })).toBeNull()
  })
  it.each(['research.revision_commit', 'research.revision_run'])('keeps reference approval separate for %s', (toolName) => {
    const creating = toolName === 'research.revision_commit'
    render(<ToolCallCard call={{ ...call, tool_name: toolName, risk: creating ? 'write' : 'execute',
      arguments: { study_id: 'study-1', [creating ? 'preview_call_id' : 'create_call_id']: 'source-call' },
      preview: { ok: true, version_matches: true, approval_matches: true,
        revision_preview: { diff: '-old\n+Agent solver', expected_state_version: 4 } },
    }} chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect(screen.getByText(creating ? '等待创建审批' : '等待执行审批')).toBeTruthy()
    expect(screen.getByRole('button', { name: creating ? '批准创建 Revision（不执行）' : '批准排队执行 Revision' })).toBeTruthy()
    expect(screen.getByText(creating ? /预览回执不等于创建批准/ : /创建批准不等于执行批准/)).toBeTruthy()
  })
  it('does not label an enqueued experiment as completed', () => {
    render(<ToolCallCard call={{ ...call, status: 'completed', tool_name: 'research.revision_run',
      result: { status: 'enqueued', job: { job_id: 'job-new', status: 'queued' } },
    }} chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect(screen.getByText('已入队，等待作业结果')).toBeTruthy()
    expect(screen.getByText(/实验尚未完成/)).toBeTruthy()
    expect(screen.queryByText('已完成')).toBeNull()
  })
  it('blocks reference execution on a stale receipt just like the legacy digest interface', () => {
    render(<ToolCallCard call={{ ...call, tool_name: 'research.revision_run',
      preview: { ok: false, approval_matches: false, reason: '引用回执已过期' } }}
      chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect((screen.getByRole('button', { name: '批准排队执行 Revision' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('引用回执已过期')).toBeTruthy()
  })
  it('separates immutable Revision creation from execution and opens its diff', () => {
    const open = vi.fn()
    const revisionCall = { ...call, tool_name: 'research.revision_create', risk: 'write',
      preview: { ok: true, version_matches: true, approval_matches: true,
        revision_preview: { diff: '-old\n+Agent solver', expected_state_version: 4 } } } as unknown as ChatState['tool_calls'][number]
    render(<ToolCallCard call={revisionCall} chatPending={false}
      onToolAction={vi.fn()} onOpenResource={open} />)
    expect(screen.getByRole('button', { name: '批准创建 Revision（不执行）' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '↗ 源码差异' }))
    expect(open).toHaveBeenCalledWith({ kind: 'diff', ref: 'tool-1', studyId: 'study-1', title: '源码差异' })
    expect(screen.getByText('Revision 源码差异').closest('details')?.open).toBe(true)
  })
  it('disables Revision execution when the immutable digest no longer matches', () => {
    const revisionCall = { ...call, tool_name: 'research.revision_execute', risk: 'write',
      preview: { ok: false, approval_matches: false, reason: 'Revision changed' } } as unknown as ChatState['tool_calls'][number]
    render(<ToolCallCard call={revisionCall} chatPending={false}
      onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect((screen.getByRole('button', { name: '批准排队执行 Revision' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('explains failed-parent repair without granting execution approval', () => {
    const repair = { ...call, tool_name: 'research.revision_create', risk: 'write',
      arguments: { parent_revision_id: 'revision-failed' }, preview: { ok: true,
        approval_matches: true, revision_preview: { parent_revision_id: 'revision-failed' } } } as unknown as ChatState['tool_calls'][number]
    render(<ToolCallCard call={repair} chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect(screen.getByText(/本次修复绑定已失败的父版本/)).toBeTruthy()
    expect(screen.getByRole('button', { name: '批准创建 Revision（不执行）' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: '批准排队执行 Revision' })).toBeNull()
  })
  it('shows the version actually read, not a refreshed preview of a changed file', () => {
    render(<ToolCallCard call={{ ...call, tool_name: 'workspace.file_read', status: 'completed',
      arguments: { path: 'notes.txt' }, preview: { ok: true, file_read: {
        path: 'notes.txt', bytes: 99, sha256: 'new-disk-version' } },
      result: { path: 'notes.txt', bytes: 42, sha256: 'executed-version' } } as unknown as ChatState['tool_calls'][number]}
      chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect(screen.getByText('executed-version')).toBeTruthy()
    expect(screen.queryByText('new-disk-version')).toBeNull()
  })
  it('opens the actual nested Job and retains its Study identity', () => {
    const open = vi.fn()
    render(<ToolCallCard call={call} chatPending={false} onToolAction={vi.fn()} onOpenResource={open} />)
    fireEvent.click(screen.getByRole('button', { name: '↗ 运行作业' }))
    expect(open).toHaveBeenCalledWith({ kind: 'job', ref: 'job-1', studyId: 'study-1', title: '运行作业' })
    expect(screen.getByText('会消费一次性确认资源，消费后不能自动重试。')).toBeTruthy()
  })
  it('blocks approved execution when the preview has become stale', () => {
    render(<ToolCallCard call={{ ...call, status: 'approved', preview: { ...call.preview!, version_matches: false } }}
      chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect((screen.getByRole('button', { name: '继续执行已批准工具' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('prevents repeated decisions while a request is unresolved', async () => {
    let done!: () => void
    const action = vi.fn(() => new Promise<void>(resolve => { done = resolve }))
    render(<ToolCallCard call={call} chatPending={false} onToolAction={action} onOpenResource={vi.fn()} />)
    const approve = screen.getByRole('button', { name: '批准排队执行 Study' })
    fireEvent.click(approve); fireEvent.click(approve)
    expect(action).toHaveBeenCalledTimes(1)
    done()
    await waitFor(() => expect((approve as HTMLButtonElement).disabled).toBe(false))
  })
  it('keeps failed approval feedback in the tool card with an explicit project', async () => {
    render(<ToolCallCard call={call} projectName="临时 ODE 研究" chatPending={false}
      onToolAction={vi.fn(async () => { throw new Error('审批版本已变化') })} onOpenResource={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '批准排队执行 Study' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('审批版本已变化'))
    expect(screen.getByText('所属项目 · 临时 ODE 研究 / Study')).toBeTruthy()
    expect(screen.getByText('查看工具参数').parentElement?.hasAttribute('open')).toBe(false)
  })
  it('separates completed development execution from a failed research threshold and opens exact evidence IDs', () => {
    const open = vi.fn()
    render(<ToolCallCard call={{ ...call, status: 'completed', tool_name: 'research.revision_job',
      result: { job: { job_id: 'job-1', status: 'succeeded' }, development_diagnostics: diagnostics } }}
      chatPending={false} onToolAction={vi.fn()} onOpenResource={open} />)
    expect(screen.getByText('未达到开发阈值')).toBeTruthy()
    expect(screen.getByText(/仅开发集.*不构成独立确认或 Claim/)).toBeTruthy()
    expect(screen.getByText(/执行完成、轨迹格式有效不等于科研达标/)).toBeTruthy()
    expect(screen.getByText(/RHS 查询数不等于全部计算成本/)).toBeTruthy()
    expect(screen.getByText('逐系统与案例诊断').closest('details')).toHaveProperty('open', false)
    expect(screen.getByText('诊断来源与对象标识').closest('details')).toHaveProperty('open', false)
    fireEvent.click(screen.getByRole('button', { name: '固定基线开发观察 ↗' }))
    expect(open).toHaveBeenCalledWith({ kind: 'observation', ref: 'control-observation', studyId: 'study-1', title: '固定基线开发观察' })
  })
  it('does not fabricate a score or threshold verdict when a failed execution has no observation', () => {
    render(<ToolCallCard call={{ ...call, status: 'completed', tool_name: 'research.revision_job',
      result: { development_diagnostics: { ...diagnostics, candidate: { execution_status: 'failed', trajectory_error: null }, control: {}, comparison: {} } } }}
      chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect(screen.getByText('候选轨迹误差').nextElementSibling?.textContent).toBe('未提供')
    expect(screen.getByText('尚无可核验判定')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /开发观察/ })).toBeNull()
  })
  it.each([{ scope: 'confirmation' }, { study_id: 'other-study' }, { job_id: 'other-job' }])('rejects out-of-scope diagnostic summaries: %o', (changed) => {
    render(<ToolCallCard call={{ ...call, status: 'completed', tool_name: 'research.revision_job',
      result: { job: { job_id: 'job-1' }, development_diagnostics: { ...diagnostics, ...changed } } }}
      chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect(screen.queryByRole('region', { name: '开发集诊断' })).toBeNull()
    expect(screen.queryByText('未达到开发阈值')).toBeNull()
  })
  it('keeps failed diagnosis local and does not claim a score when artifact verification fails', () => {
    render(<ToolCallCard call={{ ...call, status: 'completed', tool_name: 'research.revision_job',
      result: { job: { job_id: 'job-1', status: 'succeeded' }, development_diagnostics_error: { message: '轨迹摘要不一致' } } }}
      chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect(screen.getByRole('status').textContent).toContain('轨迹摘要不一致')
    expect(screen.getByRole('button', { name: '↗ 运行作业' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: '开发集诊断' })).toBeNull()
  })
  it('keeps zero measurements, rejects non-finite scores and renders source strings only as text', () => {
    render(<ToolCallCard call={{ ...call, status: 'completed', tool_name: 'research.revision_job',
      result: { development_diagnostics: { ...diagnostics, candidate: { trajectory_error: 0, trusted_rhs_queries: 0, cases: [
        { id: '<script>execute()</script>', system: 'harmonic', failed: true, trajectory_error: null, energy_drift: null } ] },
      control: { trajectory_error: Number.POSITIVE_INFINITY }, comparison: { development_threshold_met: true, relative_improvement: null } } } }}
      chatPending={false} onToolAction={vi.fn()} onOpenResource={vi.fn()} />)
    expect(screen.getByText('候选轨迹误差').nextElementSibling?.textContent).toBe('0')
    expect(screen.getByText('可信 RHS 查询').nextElementSibling?.textContent).toBe('0')
    expect(screen.getByText('固定基线轨迹误差').nextElementSibling?.textContent).toBe('未提供')
    expect(screen.getByText('达到开发阈值（仍需独立确认）')).toBeTruthy()
    expect(screen.getByText(/<script>execute\(\)<\/script> · 谐振子/)).toBeTruthy()
    expect(document.querySelector('script')).toBeNull()
  })
  it('opens a completed Revision preview from its persisted result when the live preview is absent', () => {
    const open = vi.fn(), action = vi.fn()
    render(<ToolCallCard call={{ ...call, tool_name: 'research.revision_preview', status: 'completed', preview: undefined,
      result: { study_id: 'study-1', path: 'model.py', preview_call_id: 'tool-1', expected_state_version: 40,
        proposal_sha256: 'f'.repeat(64), diff: '-original\n+stored Agent source' } }}
      chatPending={false} onToolAction={action} onOpenResource={open} />)
    fireEvent.click(screen.getByRole('button', { name: '↗ 源码差异' }))
    expect(open).toHaveBeenCalledWith({ kind: 'diff', ref: 'tool-1', studyId: 'study-1', title: '源码差异' })
    expect(screen.getByText((_, element) => element?.tagName === 'PRE' && element.textContent === '-original\n+stored Agent source')).toBeTruthy()
    expect(screen.getByText(/仅完成预览，不代表已创建 Revision/)).toBeTruthy()
    expect(screen.getByText('Revision 源码差异 · 历史预览回执').closest('details')).toHaveProperty('open', false)
    expect(screen.getByText('f'.repeat(64))).toBeTruthy()
    expect(screen.queryByRole('button', { name: /批准|继续执行/ })).toBeNull()
    expect(action).not.toHaveBeenCalled()
  })
})
