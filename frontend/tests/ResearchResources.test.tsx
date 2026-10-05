import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { BoundManuscriptPanel, EvidencePanel, JobPanel } from '../src/components/ResearchResources'
import { ConfirmProvider } from '../src/components/ConfirmProvider'
import { cancelStudyJob, fetchBoundDraft, fetchDesignJob, fetchResearchEvidence, fetchRevisionJob, fetchStudyJobs, reviewBoundDraft, type CreatedBoundDraft, type ResearchEvidencePreview, type ResearchRunState, type ResearchState, type StudyLifecycleJob } from '../src/api'
import type { WorkbenchTab } from '../src/workbench'
import type { ChatState } from '../src/types'

vi.mock('../src/api', () => ({ fetchResearchEvidence: vi.fn(), fetchBoundDraft: vi.fn(), fetchBoundDrafts: vi.fn(), fetchStudyJobs: vi.fn(), fetchDesignJob: vi.fn(), fetchRevisionJob: vi.fn(), reviewBoundDraft: vi.fn(), cancelStudyJob: vi.fn() }))

function deferred<T>() { let resolve!: (value: T) => void, reject!: (error: Error) => void; const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail }); return { promise, resolve, reject } }
const run = { run_dir: '/synthetic/study', ok: true, manifest: { study_id: 'study-1' }, study: { question: '合成研究问题' }, state_version: 10, claims: [{ entity_id: 'claim-1', metric: 'error', oriented_delta: 0.2, status: 'supports_threshold' }], observations: [] } as unknown as ResearchRunState
const research = { runs: [run], status_labels: {}, trusted_local: true } as ResearchState
const evidence = (version = 10, studyId = 'study-1', marker = 'CURRENT VERIFIED CONTENT'): ResearchEvidencePreview => ({ bundle: { study_id: studyId, study_state_version: version, verification_level: 'local', bindings: [{ claim_id: 'claim-1' }] }, markdown: marker, json_text: '{}' })
const draft = (text = 'Synthetic manuscript'): CreatedBoundDraft => ({ status: 'draft_needs_review', draft_id: 'draft-1', claim_count: 1, draft_dir: 'draft', evidence_dir: 'evidence', draft_spec_path: 'spec', manuscript_path: 'draft/manuscript.md', manuscript: text, publication_attested: false, flow_state: 'draft' })
const job = (extra: Partial<StudyLifecycleJob> = {}): StudyLifecycleJob => ({ job_id: 'job-1', status: 'succeeded', request: { study_id: 'study-1', action: 'confirm', max_steps: 1 }, cancel_requested: false, error: null, result: { phase: 'concluded', observation_ids: ['observation-actual'], claim_ids: ['claim-actual'] }, ...extra })
const tab = { id: 'job-tab', kind: 'job', ref: 'job-1', studyId: 'study-1', title: '作业' } as WorkbenchTab
const diagnosticJob = (diagnosticChanges: Record<string, unknown> = {}) => ({
  job_id: 'job-1', revision_id: 'revision-1', revision_manifest_sha256: 'revision-hash', status: 'succeeded', attempt: 1, cancel_requested: 0,
  observation_id: 'candidate-observation', error: null, created: '', updated: '',
  development_diagnostics: { schema_version: '1.0', scope: 'dev', study_id: 'study-1', state_version: 10, job_id: 'job-1', revision_id: 'revision-1', run_id: 'run-1',
    candidate: { observation_id: 'candidate-observation', trajectory_error: 4, execution_status: 'succeeded', execution_cases: 32, trajectory_valid_cases: 32, trusted_rhs_queries: 327680 },
    control: { observation_id: 'control-observation', trajectory_error: 0.15 }, comparison: { development_threshold_met: false, relative_improvement: -25.666 },
    current_budget: { candidate_development: { remaining: 2 } }, ...diagnosticChanges },
} as unknown as Awaited<ReturnType<typeof fetchRevisionJob>>)

beforeEach(() => {
  vi.mocked(fetchResearchEvidence).mockReset().mockResolvedValue(evidence())
  vi.mocked(fetchBoundDraft).mockReset().mockResolvedValue(draft())
  vi.mocked(fetchStudyJobs).mockReset().mockResolvedValue({ jobs: [job()] })
  vi.mocked(fetchDesignJob).mockReset().mockRejectedValue(new Error('Not a design job'))
  vi.mocked(fetchRevisionJob).mockReset().mockRejectedValue(new Error('Not a revision job'))
  vi.mocked(reviewBoundDraft).mockReset().mockResolvedValue({ status: 'reviewed' })
  vi.mocked(cancelStudyJob).mockReset()
})

describe('Research resource context and verification', () => {
  it('deduplicates verification and ignores a late result from the old workspace', async () => {
    const old = deferred<ResearchEvidencePreview>()
    vi.mocked(fetchResearchEvidence).mockReturnValueOnce(old.promise)
    const open = vi.fn()
    const { rerender } = render(<EvidencePanel workspaceId="project-1" run={run} onOpenResource={open} />)
    const verify = screen.getByRole('button', { name: '核验证据绑定' })
    fireEvent.click(verify); fireEvent.click(verify)
    expect(fetchResearchEvidence).toHaveBeenCalledTimes(1)
    rerender(<EvidencePanel workspaceId="project-2" run={run} onOpenResource={open} />)
    await act(async () => { old.resolve(evidence(10, 'study-1', 'OLD PROJECT CONTENT')); await old.promise })
    expect(screen.queryByText('OLD PROJECT CONTENT')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '核验证据绑定' }))
    expect(await screen.findByText('CURRENT VERIFIED CONTENT')).toBeTruthy()
  })
  it('requires re-verification on a state version mismatch and after a version update', async () => {
    vi.mocked(fetchResearchEvidence).mockResolvedValueOnce(evidence(9))
    const { rerender } = render(<EvidencePanel workspaceId="project" run={run} onOpenResource={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '核验证据绑定' }))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '核验版本与当前研究不一致，需重新核验。')
    expect(screen.queryByText('CURRENT VERIFIED CONTENT')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '核验证据绑定' }))
    expect(await screen.findByText('CURRENT VERIFIED CONTENT')).toBeTruthy()
    rerender(<EvidencePanel workspaceId="project" run={{ ...run, state_version: 11 }} onOpenResource={vi.fn()} />)
    expect(screen.queryByText('CURRENT VERIFIED CONTENT')).toBeNull()
    expect(screen.getByText(/需重新核验：当前研究/)).toBeTruthy()
  })
  it('does not surface a late verification error after switching studies', async () => {
    const old = deferred<ResearchEvidencePreview>()
    vi.mocked(fetchResearchEvidence).mockReturnValueOnce(old.promise)
    const { rerender } = render(<EvidencePanel workspaceId="project" run={run} onOpenResource={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '核验证据绑定' }))
    rerender(<EvidencePanel workspaceId="project" run={{ ...run, manifest: { ...run.manifest!, study_id: 'study-2' } }} onOpenResource={vi.fn()} />)
    await act(async () => { old.reject(new Error('OLD FAILURE')); try { await old.promise } catch { /* expected old request */ } })
    expect(screen.queryByText('OLD FAILURE')).toBeNull()
  })
  it('never revives an old verification when returning to the original workspace', async () => {
    const old = deferred<ResearchEvidencePreview>()
    vi.mocked(fetchResearchEvidence).mockReturnValueOnce(old.promise)
    const { rerender } = render(<EvidencePanel workspaceId="project-1" run={run} onOpenResource={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '核验证据绑定' }))
    rerender(<EvidencePanel workspaceId="project-2" run={run} onOpenResource={vi.fn()} />)
    rerender(<EvidencePanel workspaceId="project-1" run={run} onOpenResource={vi.fn()} />)
    await act(async () => { old.resolve(evidence(10, 'study-1', 'STALE FIRST VISIT')); await old.promise })
    expect(screen.queryByText('STALE FIRST VISIT')).toBeNull()
  })
})

describe('Evidence-bound manuscript review', () => {
  const panel = (extra: Partial<Parameters<typeof BoundManuscriptPanel>[0]> = {}) => <ConfirmProvider><BoundManuscriptPanel workspaceId="project" studyId="study-1" draftId="draft-1" stateVersion={10} trustedLocal onOpenResource={vi.fn()} {...extra} /></ConfirmProvider>
  it('reviews in the same read-only panel, deduplicates writes and retains reasons on failure', async () => {
    const review = deferred<{ status: string }>()
    vi.mocked(reviewBoundDraft).mockReturnValueOnce(review.promise)
    render(panel())
    expect(await screen.findByText('Synthetic manuscript')).toBeTruthy()
    expect(screen.getByText(/只读证据快照/)).toBeTruthy()
    const input = screen.getByRole('textbox', { name: '审校理由（必填）' })
    fireEvent.change(input, { target: { value: 'Checked claims and author limitations.' } })
    const submit = screen.getByRole('button', { name: '登记人工审校' })
    fireEvent.click(submit); fireEvent.click(submit)
    const confirm = await screen.findByRole('button', { name: '确认登记审校' })
    fireEvent.click(confirm); fireEvent.click(confirm)
    await waitFor(() => expect(reviewBoundDraft).toHaveBeenCalledTimes(1))
    expect(reviewBoundDraft).toHaveBeenCalledWith('study-1', 'draft-1', 'Checked claims and author limitations.')
    await act(async () => { review.reject(new Error('Synthetic write conflict')); try { await review.promise } catch { /* expected write error */ } })
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('审校理由已保留'))
    expect(input).toHaveProperty('value', 'Checked claims and author limitations.')
  })
  it('registers a successful review without claiming publication', async () => {
    vi.mocked(fetchBoundDraft).mockResolvedValueOnce(draft()).mockResolvedValueOnce({ ...draft(), flow_state: 'reviewed' })
    render(panel())
    await screen.findByText('Synthetic manuscript')
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Reviewed all source evidence.' } })
    fireEvent.click(screen.getByRole('button', { name: '登记人工审校' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认登记审校' }))
    expect(await screen.findByText(/此稿件已登记人工审校；该状态不代表/)).toBeTruthy()
    expect(screen.queryByRole('textbox')).toBeNull()
  })
  it('blocks review when evidence versions differ and never accepts stale manuscript reads', async () => {
    const old = deferred<CreatedBoundDraft>()
    vi.mocked(fetchBoundDraft).mockReturnValueOnce(old.promise).mockResolvedValueOnce(draft('NEW PROJECT MANUSCRIPT'))
    vi.mocked(fetchResearchEvidence).mockResolvedValue(evidence(9))
    const { rerender } = render(panel())
    rerender(panel({ workspaceId: 'new-project' }))
    expect(await screen.findByText('NEW PROJECT MANUSCRIPT')).toBeTruthy()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Review reason' } })
    expect(screen.getByRole('button', { name: '登记人工审校' })).toHaveProperty('disabled', true)
    await act(async () => { old.resolve(draft('OLD MANUSCRIPT')); await old.promise })
    expect(screen.queryByText('OLD MANUSCRIPT')).toBeNull()
    expect(screen.getByText(/需重新核验：证据返回版本/)).toBeTruthy()
  })
  it('does not submit a confirmation accepted after switching project context', async () => {
    const { rerender } = render(panel())
    await screen.findByText('Synthetic manuscript')
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Reason before switching' } })
    fireEvent.click(screen.getByRole('button', { name: '登记人工审校' }))
    await screen.findByRole('dialog')
    rerender(panel({ workspaceId: 'new-project' }))
    fireEvent.click(screen.getByRole('button', { name: '确认登记审校' }))
    await act(async () => { await Promise.resolve() })
    expect(reviewBoundDraft).not.toHaveBeenCalled()
  })
})

describe('Job summaries and result navigation', () => {
  it('opens exact result object IDs and keeps technical JSON collapsed', async () => {
    const open = vi.fn()
    const { container } = render(<ConfirmProvider><JobPanel workspaceId="project" tab={tab} chat={null} research={research} onOpenResource={open} /></ConfirmProvider>)
    fireEvent.click(await screen.findByRole('button', { name: '实验观察 1 ↗' }))
    expect(open).toHaveBeenCalledWith({ kind: 'observation', ref: 'observation-actual', studyId: 'study-1', title: '实验观察 1' })
    fireEvent.click(screen.getByRole('button', { name: '研究结论 1 ↗' }))
    expect(open).toHaveBeenCalledWith({ kind: 'claim', ref: 'claim-actual', studyId: 'study-1', title: '研究结论 1' })
    expect(container.querySelector('details')).toHaveProperty('open', false)
  })
  it('shows requested cancellation as pending until the backend returns a terminal status', async () => {
    const running = job({ status: 'running', result: null })
    const cancelling = { ...running, cancel_requested: true }
    vi.mocked(fetchStudyJobs).mockResolvedValueOnce({ jobs: [running] }).mockResolvedValue({ jobs: [cancelling] })
    vi.mocked(cancelStudyJob).mockResolvedValue(cancelling)
    render(<ConfirmProvider><JobPanel workspaceId="project" trustedLocal tab={tab} chat={null} research={research} onOpenResource={vi.fn()} /></ConfirmProvider>)
    const cancel = await screen.findByRole('button', { name: '请求取消' })
    fireEvent.click(cancel); fireEvent.click(cancel)
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(dialog.querySelector('button.danger')!)
    expect(await screen.findByText(/已请求取消，正在等待作业安全结束/)).toBeTruthy()
    expect(screen.getByRole('button', { name: '等待取消结果' })).toHaveProperty('disabled', true)
    expect(cancelStudyJob).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('已取消')).toBeNull()
  })
  it('retains the old design-job fallback with explicit observation IDs', async () => {
    vi.mocked(fetchStudyJobs).mockResolvedValue({ jobs: [] })
    vi.mocked(fetchDesignJob).mockResolvedValue({ job_id: 'job-1', design_id: 'design', design_sha256: 'hash', status: 'succeeded', attempt: 1, cancel_requested: 0, observation_id: 'legacy-observation', error: null, created: '', updated: '' })
    const open = vi.fn()
    render(<ConfirmProvider><JobPanel workspaceId="project" tab={tab} chat={null} research={research} onOpenResource={open} /></ConfirmProvider>)
    fireEvent.click(await screen.findByRole('button', { name: '实验观察 1 ↗' }))
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ ref: 'legacy-observation', studyId: 'study-1' }))
    expect(screen.getByRole('heading', { name: '实验设计执行' })).toBeTruthy()
  })
  it('renders structured Agent errors as meaningful text', async () => {
    const chat = { jobs: [{ job_id: 'job-1', status: 'failed', turn_id: 'turn-1', total_tokens: 25, error: { type: 'ProtocolError', message: 'Synthetic stream interrupted' } }] } as unknown as ChatState
    render(<ConfirmProvider><JobPanel workspaceId="project" tab={{ ...tab, studyId: undefined }} chat={chat} research={research} onOpenResource={vi.fn()} /></ConfirmProvider>)
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Synthetic stream interrupted')
    expect(screen.getByRole('heading', { name: 'Agent 回复生成' })).toBeTruthy()
    expect(screen.queryByText('[object Object]')).toBeNull()
  })
  it('drops old job details after a workspace or research version change', async () => {
    const old = deferred<{ jobs: StudyLifecycleJob[] }>()
    vi.mocked(fetchStudyJobs).mockReturnValueOnce(old.promise).mockResolvedValue({ jobs: [job({ result: { phase: 'concluded', observation_ids: ['current-observation'], claim_ids: [] } })] })
    const open = vi.fn()
    const { rerender } = render(<ConfirmProvider><JobPanel workspaceId="project-1" tab={tab} chat={null} research={research} onOpenResource={open} /></ConfirmProvider>)
    rerender(<ConfirmProvider><JobPanel workspaceId="project-2" tab={tab} chat={null} research={{ ...research, runs: [{ ...run, state_version: 11 }] }} onOpenResource={open} /></ConfirmProvider>)
    const current = await screen.findByRole('button', { name: '实验观察 1 ↗' })
    await act(async () => { old.resolve({ jobs: [job()] }); await old.promise })
    fireEvent.click(current)
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ ref: 'current-observation' }))
    expect(screen.queryByRole('button', { name: '研究结论 1 ↗' })).toBeNull()
  })
  it('shows optional verified development diagnostics without mistaking execution success for scientific success', async () => {
    vi.mocked(fetchStudyJobs).mockResolvedValue({ jobs: [] })
    vi.mocked(fetchRevisionJob).mockResolvedValue(diagnosticJob())
    const open = vi.fn()
    const { container } = render(<ConfirmProvider><JobPanel workspaceId="project" tab={tab} chat={null} research={research} onOpenResource={open} /></ConfirmProvider>)
    expect(await screen.findByText('未达到开发阈值')).toBeTruthy()
    expect(screen.getByText('状态').nextElementSibling?.textContent).toBe('执行成功')
    expect(screen.getByText('候选开发次数剩余').nextElementSibling?.textContent).toBe('2')
    expect(container.querySelector('section[aria-label="开发集诊断"] details')).toHaveProperty('open', false)
    fireEvent.click(screen.getByRole('button', { name: '候选开发观察 ↗' }))
    expect(open).toHaveBeenCalledWith({ kind: 'observation', ref: 'candidate-observation', studyId: 'study-1', title: '候选开发观察' })
  })
  it('labels an old diagnostic snapshot and never presents its budget as current', async () => {
    vi.mocked(fetchStudyJobs).mockResolvedValue({ jobs: [] })
    vi.mocked(fetchRevisionJob).mockResolvedValue(diagnosticJob({ state_version: 9 }))
    render(<ConfirmProvider><JobPanel workspaceId="project" tab={tab} chat={null} research={research} onOpenResource={vi.fn()} /></ConfirmProvider>)
    expect(await screen.findByText(/历史诊断快照：研究版本已变化/)).toBeTruthy()
    expect(screen.queryByText('候选开发次数剩余')).toBeNull()
    expect(screen.getByText('未达到开发阈值')).toBeTruthy()
  })
  it('uses the exact diagnostic candidate Observation when the original queued job has no result IDs', async () => {
    vi.mocked(fetchStudyJobs).mockResolvedValue({ jobs: [] })
    vi.mocked(fetchRevisionJob).mockResolvedValue({ ...diagnosticJob(), observation_id: undefined, result: undefined } as unknown as Awaited<ReturnType<typeof fetchRevisionJob>>)
    const open = vi.fn()
    render(<ConfirmProvider><JobPanel workspaceId="project" tab={tab} chat={null} research={research} onOpenResource={open} /></ConfirmProvider>)
    fireEvent.click(await screen.findByRole('button', { name: '实验观察 1 ↗' }))
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ ref: 'candidate-observation', studyId: 'study-1' }))
    expect(screen.queryByText(/此作业没有登记可直接打开/)).toBeNull()
  })
  it('ignores a delayed development diagnostic from the first visit to a project', async () => {
    vi.mocked(fetchStudyJobs).mockResolvedValue({ jobs: [] })
    const old = deferred<Awaited<ReturnType<typeof fetchRevisionJob>>>()
    vi.mocked(fetchRevisionJob).mockReturnValueOnce(old.promise).mockResolvedValue(diagnosticJob({ candidate: { trajectory_error: 7 } }))
    const panel = (project: string) => <ConfirmProvider><JobPanel workspaceId={project} tab={tab} chat={null} research={research} onOpenResource={vi.fn()} /></ConfirmProvider>
    const { rerender } = render(panel('project-1'))
    await waitFor(() => expect(fetchRevisionJob).toHaveBeenCalledTimes(1))
    rerender(panel('project-2'))
    expect(await screen.findByText('候选轨迹误差')).toBeTruthy()
    rerender(panel('project-1'))
    await waitFor(() => expect(fetchRevisionJob).toHaveBeenCalledTimes(3))
    await act(async () => { old.resolve(diagnosticJob({ candidate: { trajectory_error: 90000 } })); await old.promise })
    expect(screen.getByText('候选轨迹误差').nextElementSibling?.textContent).toBe('7.00000')
  })
  it('leaves job records available when the optional diagnosis fails', async () => {
    vi.mocked(fetchStudyJobs).mockResolvedValue({ jobs: [] })
    vi.mocked(fetchRevisionJob).mockResolvedValue({ ...diagnosticJob(), development_diagnostics: undefined,
      development_diagnostics_error: { message: '计量回执校验失败' } } as unknown as Awaited<ReturnType<typeof fetchRevisionJob>>)
    render(<ConfirmProvider><JobPanel workspaceId="project" tab={tab} chat={null} research={research} onOpenResource={vi.fn()} /></ConfirmProvider>)
    expect(await screen.findByRole('status')).toHaveProperty('textContent', expect.stringContaining('计量回执校验失败'))
    expect(screen.getByRole('button', { name: '实验观察 1 ↗' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: '开发集诊断' })).toBeNull()
  })
})
