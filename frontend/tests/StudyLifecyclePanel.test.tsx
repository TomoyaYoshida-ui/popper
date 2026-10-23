import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StudyLifecyclePanel } from '../src/components/StudyLifecyclePanel'
import { cancelStudyJob, executeStudyAction, fetchStudyJobs, previewStudyAction, type StudyActionPreview, type StudyLifecycleJob } from '../src/api'

vi.mock('../src/api', () => ({ cancelStudyJob: vi.fn(), executeStudyAction: vi.fn(), fetchStudyJobs: vi.fn(), previewStudyAction: vi.fn() }))
const preview: StudyActionPreview = {
  study_id: 'study-A', action: 'run', max_steps: 1, state_version: 5, approval_sha256: 'original-approval',
  objective: 'Synthetic study', metric: { name: 'error', direction: 'min' }, selected_hypothesis_id: 'candidate-1',
  scope: 'dev', confirmation_cost: 0, budget: { available: 4 }, stop_conditions: ['预算耗尽', '用户停止'], confirmation_warning: null,
}
const job: StudyLifecycleJob = { job_id: 'job-A', status: 'running', request: { study_id: 'study-A', action: 'run', max_steps: 1 },
  cancel_requested: false, error: null, result: null }
const base = { studyId: 'study-A', projectKey: 'project-A', stateVersion: 5, phase: 'researching', trustedLocal: true, onChanged: vi.fn() }
beforeEach(() => {
  vi.mocked(fetchStudyJobs).mockResolvedValue({ jobs: [] })
  vi.mocked(previewStudyAction).mockResolvedValue(preview)
  vi.mocked(executeStudyAction).mockResolvedValue(job)
  vi.mocked(cancelStudyJob).mockResolvedValue({ ...job, cancel_requested: true })
})
async function loaded(label = '预览研究运行') {
  await waitFor(() => expect((screen.getByRole('button', { name: label }) as HTMLButtonElement).disabled).toBe(false))
}
describe('server-directed Study actions', () => {
  it('presents one next action for the service phase rather than guessing from candidates', async () => {
    const { rerender } = render(<StudyLifecyclePanel {...base} />)
    await loaded()
    expect(screen.queryByRole('button', { name: '预览独立确认' })).toBeNull()
    rerender(<StudyLifecyclePanel {...base} phase="ready_for_confirmation" />)
    await loaded('预览独立确认')
    expect(screen.queryByRole('button', { name: '预览研究运行' })).toBeNull()
  })
  it('does not infer execution eligibility when the optional phase is unavailable', () => {
    render(<StudyLifecyclePanel {...base} phase={undefined} />)
    expect(screen.queryByRole('button', { name: '预览研究运行' })).toBeNull()
    expect(screen.getByText(/不会根据候选/)).toBeTruthy()
  })
  it('keeps execution disabled when job status cannot be verified and offers a local retry', async () => {
    vi.mocked(fetchStudyJobs).mockRejectedValueOnce(new Error('synthetic disconnection'))
    render(<StudyLifecyclePanel {...base} />)
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('synthetic disconnection'))
    expect((screen.getByRole('button', { name: '预览研究运行' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '重新核对状态' }))
    await loaded()
    expect(screen.queryByRole('alert')).toBeNull()
  })
  it('blocks consumed resources and signed external confirmation with visible reasons', async () => {
    const { rerender } = render(<StudyLifecyclePanel {...base} phase="ready_for_confirmation" resourceConsumed />)
    await waitFor(() => expect(screen.queryByText('正在核对作业状态…')).toBeNull())
    expect((screen.getByRole('button', { name: '预览独立确认' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/一次性确认资源已消费；不能创建/)).toBeTruthy()
    rerender(<StudyLifecyclePanel {...base} phase="ready_for_confirmation" confirmationMode="signed_external" />)
    expect((screen.getByRole('button', { name: '预览独立确认' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/此研究使用外部签名确认/)).toBeTruthy()
    expect(previewStudyAction).not.toHaveBeenCalled()
  })
  it('keeps failed confirmation non-retryable and directs conclusions to evidence/manuscripts', async () => {
    const open = vi.fn()
    const { rerender } = render(<StudyLifecyclePanel {...base} phase="confirmation_failed" onOpenResource={open} />)
    expect(screen.getByText(/已消费的一次性资源不得重试/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /预览/ })).toBeNull()
    rerender(<StudyLifecyclePanel {...base} phase="concluded" onOpenResource={open} />)
    fireEvent.click(screen.getByRole('button', { name: '核验证据' }))
    fireEvent.click(screen.getByRole('button', { name: '阅读与审校稿件' }))
    expect(open.mock.calls.map(call => call[0])).toEqual([
      { kind: 'evidence', studyId: 'study-A', title: '研究证据' }, { kind: 'manuscripts', studyId: 'study-A', title: '研究稿件' },
    ])
  })
  it('shows exact approval scope, version, stop conditions and preserves approval identity', async () => {
    const confirmation = { ...preview, action: 'confirm' as const, scope: 'registered_independent_confirmation', confirmation_cost: 2,
      confirmation_warning: '确认数据只消费一次；执行开始后失败或中断不会自动重试' }
    vi.mocked(previewStudyAction).mockResolvedValue(confirmation)
    render(<StudyLifecyclePanel {...base} phase="ready_for_confirmation" />)
    await loaded('预览独立确认')
    fireEvent.click(screen.getByRole('button', { name: '预览独立确认' }))
    await waitFor(() => expect(screen.getByText(/范围：预注册独立确认范围 · 研究状态版本：5/)).toBeTruthy())
    expect(screen.getByText(/停止条件：预算耗尽；用户停止/)).toBeTruthy()
    expect(screen.getByText(/选定候选：candidate-1 · 本次确认评分预算：2/)).toBeTruthy()
    expect(screen.getByText(/执行开始后失败或中断不会自动重试/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '批准并执行独立确认' }))
    await waitFor(() => expect(executeStudyAction).toHaveBeenCalledWith(confirmation))
  })
  it('suppresses duplicate writes synchronously and retains failed input for explicit retry', async () => {
    let reject!: (cause: Error) => void
    vi.mocked(previewStudyAction).mockImplementationOnce(() => new Promise((_, fail) => { reject = fail }))
    render(<StudyLifecyclePanel {...base} />)
    await loaded()
    fireEvent.change(screen.getByLabelText('本次候选步数'), { target: { value: '3' } })
    const button = screen.getByRole('button', { name: '预览研究运行' })
    fireEvent.click(button); fireEvent.click(button)
    expect(previewStudyAction).toHaveBeenCalledTimes(1)
    reject(new Error('synthetic failure'))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('synthetic failure'))
    expect((screen.getByLabelText('本次候选步数') as HTMLInputElement).value).toBe('3')
    expect(executeStudyAction).not.toHaveBeenCalled()
  })
  it('discards late preview responses after a Study/version context change', async () => {
    let resolve!: (value: StudyActionPreview) => void
    vi.mocked(previewStudyAction).mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const { rerender } = render(<StudyLifecyclePanel {...base} />)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: '预览研究运行' }))
    rerender(<StudyLifecyclePanel {...base} studyId="study-B" stateVersion={9} />)
    resolve(preview)
    await loaded()
    expect(screen.queryByText('Synthetic study')).toBeNull()
    expect(screen.queryByRole('button', { name: '批准并运行研究' })).toBeNull()
  })
  it('shows the locked assertion instead of calling a lower error scientific support', async () => {
    render(<StudyLifecyclePanel {...base} phase="concluded" claimCard={{
      feature: 'dose', label: 'dose', direction: 'negative', ci95: [-1.2, -0.4],
      scientific_status: 'supported', n_rows: 20,
      sentence: '断言获得独立证据支持。断言为dose与目标呈负向；确认区间 [-1.2, -0.4]；开发记录（不是支持依据）：搜索集基线 MAE 13、候选 MAE 4；确认行数 20。这不建立机制。',
    }} />)
    const card = screen.getByLabelText('断言卡')
    expect(card.textContent).toContain('dose')
    expect(card.textContent).toContain('负向')
    expect(card.textContent).toContain('[-1.2,-0.4]')
    expect(card.textContent).toContain('科学状态：支持')
    expect(card.textContent).not.toContain('预测更准')
    expect(card.textContent).not.toContain('迁移规则')
  })
  it('shows the declared transport rule with the locked assertion', async () => {
    render(<StudyLifecyclePanel {...base} phase="concluded" claimCard={{
      feature: 'dose', label: 'dose', direction: 'negative', ci95: [-0.2, 0.3],
      scientific_status: 'inconclusive', n_rows: 20, transportText: '搜索 batch ≤ 10，确认 batch > 10',
      sentence: '证据不足。断言为dose与目标呈负向；这是预先声明的人群迁移确认：确认人群为 batch > 10，不是同一人群的随机留出；确认区间 [-0.2, 0.3]；开发记录（不是支持依据）：搜索集基线 MAE 13、候选 MAE 4；确认集基线 MAE 100、候选 MAE 1；确认行数 20。这不建立机制。',
    }} />)
    const card = screen.getByLabelText('断言卡')
    expect(card.textContent).toContain('迁移规则：搜索 batch ≤ 10，确认 batch > 10')
    expect(card.textContent).toContain('dose')
    expect(card.textContent).toContain('负向')
    expect(card.textContent).toContain('[-0.2,0.3]')
    expect(card.textContent).toContain('科学状态：证据不足')
    expect(card.textContent).toContain('确认人群为 batch > 10')
    expect(card.textContent).toContain('这不建立机制。')
  })
  it('shows the finished search tree and the confirmation button without a support verdict', async () => {
    vi.mocked(fetchStudyJobs).mockResolvedValue({
      jobs: [],
      search_progress: {
        step: 2, max_steps: 8, nodes: 2, buggy: 1, best_value: 0.42,
        dev_runs_used: 2, max_dev_runs: 8, tokens_used: 10, max_model_tokens: 100,
        node_summaries: [
          { id: 'node-0', parent_id: null, kind: 'draft', status: 'buggy', value: null, error_type: 'SyntaxError' },
          { id: 'node-1', parent_id: 'node-0', kind: 'debug', status: 'scored', value: 0.42, error_type: null },
        ],
      },
    })
    render(<StudyLifecyclePanel {...base} phase="ready_for_confirmation" />)
    await waitFor(() => expect(screen.getByLabelText('代码树搜索')).toBeTruthy())
    expect(screen.getByText(/当前最好开发分 0.42/)).toBeTruthy()
    expect(screen.getByText(/确认前只显示开发分/)).toBeTruthy()
    expect(screen.getByRole('button', { name: '预览独立确认' })).toBeTruthy()
    expect(screen.queryByText('在声明范围内获得支持')).toBeNull()
  })
  it('prioritizes active jobs and does not mark cancellation as completion', async () => {
    vi.mocked(fetchStudyJobs).mockResolvedValue({ jobs: [job] })
    const changed = vi.fn(), open = vi.fn()
    render(<StudyLifecyclePanel {...base} onChanged={changed} onOpenResource={open} />)
    await waitFor(() => expect(screen.getByText('查看正在执行的作业')).toBeTruthy())
    expect(screen.queryByRole('button', { name: '预览研究运行' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '查看作业详情' }))
    expect(open).toHaveBeenCalledWith({ kind: 'job', ref: 'job-A', studyId: 'study-A', title: '研究运行作业' })
    fireEvent.click(screen.getByRole('button', { name: '请求停止' }))
    await waitFor(() => expect(screen.getByText(/尚未结束/)).toBeTruthy())
    expect((screen.getByRole('button', { name: '请求停止' }) as HTMLButtonElement).disabled).toBe(true)
    expect(changed).not.toHaveBeenCalled()
    expect(screen.queryByText('已停止')).toBeNull()
  })
})
