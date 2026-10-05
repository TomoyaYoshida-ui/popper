import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { FileEmptyState, FileTree } from '../src/components/FileTree'
import { listProjectFiles } from '../src/api'

vi.mock('../src/api', () => ({ listProjectFiles: vi.fn() }))
const files = [{ path: 'code/model.py', editable: false, bytes: 10 }, { path: 'notes.md', editable: true, bytes: 5 }, { path: 'code/config.json', editable: true, bytes: 8 }]
const props = { workspaceId: 'A', trustedLocal: true, onOpenResource: vi.fn() }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(listProjectFiles).mockResolvedValue({ files, truncated: false }) })
describe('right-side project explorer', () => {
  it('shows a calm empty editor without another input or file list', () => {
    render(<FileEmptyState />)
    expect(screen.getByRole('heading', { name: '打开文件' })).toBeTruthy()
    expect(screen.getByText('从右侧项目目录中选择文件')).toBeTruthy()
    expect(screen.queryByRole('textbox')).toBeNull()
  })
  it('expands folders and opens an explicit file resource; active paths reveal parents', async () => {
    const view = render(<FileTree {...props} />)
    const folder = await screen.findByRole('button', { name: 'code' })
    expect(folder.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('button', { name: /model.py/ })).toBeNull()
    fireEvent.click(folder)
    fireEvent.click(screen.getByRole('button', { name: /model.py/ }))
    expect(props.onOpenResource).toHaveBeenCalledWith({ kind: 'file', ref: 'code/model.py', title: 'model.py' })
    view.rerender(<FileTree {...props} activePath="code/model.py" />)
    expect(screen.getByRole('button', { name: /model.py/ }).getAttribute('aria-current')).toBe('page')
    expect(screen.getByLabelText('只读')).toBeTruthy()
  })
  it('search retains parent folders and clearing restores expansion choices', async () => {
    render(<FileTree {...props} />)
    await screen.findByRole('button', { name: 'code' })
    fireEvent.change(screen.getByLabelText('筛选项目文件'), { target: { value: 'MODEL' } })
    expect(screen.getByRole('button', { name: 'code' }).getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('button', { name: /model.py/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'notes.md' })).toBeNull()
    fireEvent.change(screen.getByLabelText('筛选项目文件'), { target: { value: '' } })
    expect(screen.getByRole('button', { name: 'code' }).getAttribute('aria-expanded')).toBe('false')
    fireEvent.change(screen.getByLabelText('筛选项目文件'), { target: { value: 'absent' } })
    expect(screen.getByText('没有匹配的文件')).toBeTruthy()
  })
  it('keeps a read failure distinct from an empty project and retries locally', async () => {
    vi.mocked(listProjectFiles).mockRejectedValueOnce(new Error('读取失败'))
    render(<FileTree {...props} />)
    await screen.findByRole('alert')
    expect(screen.queryByText('没有可展示的文本文件')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await screen.findByRole('button', { name: 'notes.md' })
    expect(screen.queryByRole('alert')).toBeNull()
  })
  it('discards a late file list after switching projects', async () => {
    let finish!: (value: Awaited<ReturnType<typeof listProjectFiles>>) => void
    vi.mocked(listProjectFiles).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    const view = render(<FileTree {...props} />)
    view.rerender(<FileTree {...props} workspaceId="B" />)
    await screen.findByRole('button', { name: 'notes.md' })
    finish({ files: [{ path: 'foreign.py', editable: true, bytes: 1 }], truncated: false })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'foreign.py' })).toBeNull())
    expect(screen.getByRole('button', { name: 'notes.md' })).toBeTruthy()
  })
})
