import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as api from '../src/api'
import { ProjectPreparation } from '../src/components/ProjectPreparation'

const confirm = vi.hoisted(() => vi.fn(async () => true))
vi.mock('../src/components/ConfirmProvider', () => ({ useConfirmation: () => confirm }))
vi.mock('../src/api', () => ({ previewProjectPreparation: vi.fn(), commitProjectPreparation: vi.fn(),
  previewStudyInitialization: vi.fn(), initializeStudy: vi.fn() }))
const preview = { project_path: 'C:/synthetic/new', name: 'Synthetic', objective: 'Synthetic ODE',
  metric: { name: 'convergence_order', direction: 'max' }, domain_pack: 'convergence-order-v1',
  entrypoint: 'model.py', code_files: ['model.py'], data_files: { train: 'train.json', dev: 'dev.json', test: 'test.json' },
  repeat_unit: 'independent_run', repeat_values: [1, 2, 3], candidate_count: 1, experiment_budget: 2,
  input_hashes: { 'model.py': 'sha' }, already_initialized: false, approval_sha256: 'approved' } as api.ProjectPreparationPreview
const protocol = { name: 'Synthetic', objective: 'Synthetic ODE', entrypoint: 'model.py', code_files: ['model.py'],
  train: 'train.json', dev: 'dev.json', test: 'test.json', metric: { name: 'convergence_order', direction: 'max', plugin_selector: 'custom' },
  baseline: { scheme: 'euler' }, candidates: [{ scheme: 'midpoint' }], budget: 4, timeout_seconds: 60,
  min_improvement: 0.5, seeds: [1, 2, 3], unknown_domain: { preserve: ['yes'] } }
function props(workspaceKey = 'project-A') { return { workspaceKey, trustedLocal: true,
  onResearchChanged: vi.fn(), onProjectChanged: vi.fn(async () => undefined), onStudyCreated: vi.fn() } }
describe('project preparation workflow', () => {
  beforeEach(() => { vi.clearAllMocks() })
  it('imports existing files without sending an empty replacement protocol', async () => {
    vi.mocked(api.previewProjectPreparation).mockResolvedValue(preview)
    render(<ProjectPreparation {...props()} />)
    expect(screen.getByRole('button', { name: '4. 建立 Study' }).hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('项目目录'), { target: { value: preview.project_path } })
    fireEvent.click(screen.getByRole('button', { name: '读取并校验项目' }))
    await screen.findByRole('heading', { name: '核对预注册范围' })
    expect(api.previewProjectPreparation).toHaveBeenCalledWith(preview.project_path, null, {})
    expect(screen.getByText(/不会执行实验/)).toBeTruthy()
    expect(api.commitProjectPreparation).not.toHaveBeenCalled()
  })
  it('offers text file rows and preserves unknown domain and nested metric fields', async () => {
    localStorage.setItem('popper-project-prep:project-A', JSON.stringify({ path: preview.project_path,
      protocol: JSON.stringify(protocol), files: '{}' }))
    vi.mocked(api.previewProjectPreparation).mockResolvedValue(preview)
    render(<ProjectPreparation {...props()} />)
    fireEvent.click(screen.getByRole('button', { name: '下一步：协议与文件' }))
    fireEvent.change(screen.getByLabelText('研究名称'), { target: { value: 'Changed title' } })
    fireEvent.click(screen.getByRole('button', { name: '＋ 添加文本文件' }))
    fireEvent.change(screen.getByLabelText('初始文件路径 1'), { target: { value: 'model.py' } })
    fireEvent.change(screen.getByLabelText('初始文件内容 1'), { target: { value: 'print("synthetic")' } })
    fireEvent.click(screen.getByRole('button', { name: '校验项目与协议' }))
    await screen.findByRole('heading', { name: '核对预注册范围' })
    expect(api.previewProjectPreparation).toHaveBeenCalledWith(preview.project_path,
      { ...protocol, name: 'Changed title' }, { 'model.py': 'print("synthetic")' })
  })
  it('does not enable Study creation until the registered target workspace is actually selected', async () => {
    vi.mocked(api.previewProjectPreparation).mockResolvedValue(preview)
    vi.mocked(api.commitProjectPreparation).mockResolvedValue({ ...preview, workspace_id: 'project-B' })
    vi.mocked(api.previewStudyInitialization).mockResolvedValue({ project: preview.project_path, run_dir: 'C:/synthetic/new/study-1',
      run_name: 'study-1', budget_cap: 4, objective: preview.objective, metric: preview.metric, candidate_count: 1,
      already_initialized: false, study_id: null, proposal_sha256: 'study-approved' })
    const first = props()
    const view = render(<ProjectPreparation {...first} />)
    fireEvent.change(screen.getByLabelText('项目目录'), { target: { value: preview.project_path } })
    fireEvent.click(screen.getByRole('button', { name: '读取并校验项目' }))
    await screen.findByRole('button', { name: '确认预注册并切换项目' })
    fireEvent.click(screen.getByRole('button', { name: '确认预注册并切换项目' }))
    await waitFor(() => expect(first.onProjectChanged).toHaveBeenCalledOnce())
    expect(screen.getByRole('button', { name: '4. 建立 Study' }).hasAttribute('disabled')).toBe(true)
    expect(api.previewStudyInitialization).not.toHaveBeenCalled()
    view.rerender(<ProjectPreparation {...first} workspaceKey="project-B" currentProjectName="Target B" />)
    await screen.findByRole('heading', { name: '为已预注册项目建立 Study' })
    expect(screen.getByRole('button', { name: '4. 建立 Study' }).hasAttribute('disabled')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '预览 Study 契约' }))
    await screen.findByRole('button', { name: '确认并登记 Study' })
    expect(api.previewStudyInitialization).toHaveBeenCalledWith('study-1', 4)
  })
  it('keeps malformed file-row drafts and rejects duplicate paths before contacting the server', async () => {
    localStorage.setItem('popper-project-prep:project-A', JSON.stringify({ path: preview.project_path,
      protocol: JSON.stringify(protocol), files: '{}', mode: 'create', stage: 'protocol' }))
    const view = render(<ProjectPreparation {...props()} />)
    fireEvent.click(screen.getByRole('button', { name: '＋ 添加文本文件' }))
    fireEvent.change(screen.getByLabelText('初始文件路径 1'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('初始文件内容 1'), { target: { value: 'unfinished file' } })
    expect(screen.getByRole('button', { name: '校验项目与协议' }).hasAttribute('disabled')).toBe(true)
    await waitFor(() => expect(JSON.parse(localStorage.getItem('popper-project-prep:project-A')!).fileRows[0].content).toBe('unfinished file'))
    view.unmount(); render(<ProjectPreparation {...props()} />)
    expect((screen.getByLabelText('初始文件内容 1') as HTMLTextAreaElement).value).toBe('unfinished file')
    expect((screen.getByLabelText('初始文件路径 1') as HTMLInputElement).value).toBe('')
  })
  it('retains the project path and reports failed validation without committing', async () => {
    vi.mocked(api.previewProjectPreparation).mockRejectedValueOnce(new Error('输入文件缺失'))
    render(<ProjectPreparation {...props()} />)
    fireEvent.change(screen.getByLabelText('项目目录'), { target: { value: preview.project_path } })
    fireEvent.click(screen.getByRole('button', { name: '读取并校验项目' }))
    await screen.findByRole('alert')
    expect(screen.getByText('输入文件缺失')).toBeTruthy()
    expect((screen.getByLabelText('项目目录') as HTMLInputElement).value).toBe(preview.project_path)
    expect(api.commitProjectPreparation).not.toHaveBeenCalled()
  })
  it('revokes the local registration shortcut after changing protocol inputs', () => {
    localStorage.setItem('popper-project-prep:project-A', JSON.stringify({ path: preview.project_path,
      protocol: JSON.stringify(protocol), files: '{}', mode: 'create', stage: 'study',
      registeredWorkspaceId: 'project-A', registeredPath: preview.project_path }))
    render(<ProjectPreparation {...props()} />)
    expect(screen.getByRole('button', { name: '4. 建立 Study' }).hasAttribute('disabled')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '2. 协议与文件' }))
    fireEvent.change(screen.getByLabelText('研究名称'), { target: { value: 'New preregistration draft' } })
    expect(screen.getByRole('button', { name: '4. 建立 Study' }).hasAttribute('disabled')).toBe(true)
    expect(api.previewStudyInitialization).not.toHaveBeenCalled()
  })
  it('serializes validation and ignores an old-workspace preview after switching', async () => {
    let finish!: (value: api.ProjectPreparationPreview) => void
    vi.mocked(api.previewProjectPreparation).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    const first = props()
    const view = render(<ProjectPreparation {...first} />)
    fireEvent.change(screen.getByLabelText('项目目录'), { target: { value: preview.project_path } })
    fireEvent.click(screen.getByRole('button', { name: '读取并校验项目' }))
    expect(screen.getByRole('button', { name: '正在校验…' }).hasAttribute('disabled')).toBe(true)
    expect(api.previewProjectPreparation).toHaveBeenCalledOnce()
    view.rerender(<ProjectPreparation {...first} workspaceKey="project-B" />)
    finish(preview)
    await waitFor(() => expect(screen.queryByRole('button', { name: '正在校验…' })).toBeNull())
    expect(screen.queryByRole('heading', { name: '核对预注册范围' })).toBeNull()
    expect((screen.getByLabelText('项目目录') as HTMLInputElement).value).toBe('')
  })
  it('does not commit or switch when project-change protection has not approved', async () => {
    vi.mocked(api.previewProjectPreparation).mockResolvedValue(preview)
    const protect = vi.fn((_action: () => void) => undefined)
    render(<ProjectPreparation {...props()} onRequestProjectChange={protect} />)
    fireEvent.change(screen.getByLabelText('项目目录'), { target: { value: preview.project_path } })
    fireEvent.click(screen.getByRole('button', { name: '读取并校验项目' }))
    await screen.findByRole('button', { name: '确认预注册并切换项目' })
    fireEvent.click(screen.getByRole('button', { name: '确认预注册并切换项目' }))
    expect(protect).toHaveBeenCalledOnce()
    expect(api.commitProjectPreparation).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '4. 建立 Study' }).hasAttribute('disabled')).toBe(true)
  })
})
