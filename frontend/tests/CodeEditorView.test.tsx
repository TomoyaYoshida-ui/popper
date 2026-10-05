import { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { CodeEditorView } from '../src/components/CodeEditorView'
import { readDraft, writeDraft } from '../src/workbench'
import { previewProjectFile, readProjectFile, saveProjectFile } from '../src/api'

vi.mock('../src/api', () => ({ readProjectFile: vi.fn(), previewProjectFile: vi.fn(), saveProjectFile: vi.fn(),
  cancelRevisionJob: vi.fn(), executeInitialRevision: vi.fn(), fetchRevisionJob: vi.fn(), listProjectFiles: vi.fn(),
  previewInitialRevision: vi.fn(), submitInitialRevision: vi.fn() }))
vi.mock('../src/components/MonacoEditor', () => ({ default: ({ value, onChange, original, readOnly }: {
  value: string; onChange: (value: string) => void; original?: string; readOnly?: boolean
}) => <><textarea aria-label="editor" readOnly={readOnly} value={value} onChange={(event) => onChange(event.target.value)} />
  {original != null && <pre aria-label="disk version">{original}</pre>}</> }))
const props = { workspaceId: 'editor-test', workspaceRoot: 'C:/test', research: null, trustedLocal: true, path: 'notes.md' }
describe('versioned editor', () => {
  it('shows file loading instead of an obsolete navigation hint and allows a failed read to retry', async () => {
    vi.mocked(readProjectFile).mockResolvedValue({ path: 'loading.md', content: 'ready', sha256: 'ready-sha', editable: true, bytes: 5 })
      .mockRejectedValueOnce(new Error('文件读取失败'))
    render(<CodeEditorView {...props} path="loading.md" />)
    expect(screen.getByRole('status').textContent).toBe('正在读取文件…')
    fireEvent.click(await screen.findByRole('button', { name: '重试读取文件' }))
    expect((await screen.findByLabelText('editor') as HTMLTextAreaElement).value).toBe('ready')
    expect(readProjectFile).toHaveBeenLastCalledWith('editor-test', 'loading.md')
    expect(saveProjectFile).not.toHaveBeenCalled()
  })
  it('retains an unresolved recovery conflict across remounts and never writes before review', async () => {
    const key = 'code:editor-test:notes.md'
    await writeDraft({ key, content: 'draft content', baseContent: 'old disk', baseSha256: 'old-sha', updated: Date.now() })
    vi.mocked(readProjectFile).mockResolvedValue({ path: 'notes.md', content: 'new disk', sha256: 'new-sha', editable: true, bytes: 8 })
    vi.mocked(previewProjectFile).mockResolvedValue({ path: 'notes.md', changed: true, before_sha256: 'new-sha', after_sha256: 'saved-sha', diff: 'diff', bytes: 13 })
    vi.mocked(saveProjectFile).mockResolvedValue({ path: 'notes.md', sha256: 'saved-sha', bytes: 13 })
    const view = render(<StrictMode><CodeEditorView {...props} /></StrictMode>)
    await screen.findByText('恢复草稿与磁盘版本存在冲突')
    expect((await screen.findByLabelText('editor') as HTMLTextAreaElement).value).toBe('draft content')
    expect(screen.getByLabelText('disk version').textContent).toBe('new disk')
    view.unmount()
    await waitFor(async () => expect((await readDraft(key))?.baseSha256).toBe('old-sha'))
    render(<CodeEditorView {...props} />)
    await screen.findByText('恢复草稿与磁盘版本存在冲突')
    expect(saveProjectFile).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '保留草稿并继续审查' }))
    fireEvent.click(screen.getByRole('button', { name: '查看差异', exact: true }))
    await waitFor(() => expect(previewProjectFile).toHaveBeenCalledWith('editor-test', 'notes.md', 'draft content', 'new-sha'))
    expect(saveProjectFile).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认差异并写回' }))
    await screen.findByText('已写回 notes.md')
    expect(saveProjectFile).toHaveBeenCalledWith('editor-test', 'notes.md', 'draft content', 'new-sha')
    expect(await readDraft(key)).toBeNull()
  })
  it('keeps frozen inputs read-only without an eligible Revision design', async () => {
    vi.mocked(readProjectFile).mockResolvedValue({ path: 'model.py', content: 'frozen', sha256: 'frozen-sha', editable: false, bytes: 6 })
    render(<CodeEditorView {...props} path="model.py" />)
    await screen.findByText('只读 · 已冻结输入')
    expect((screen.getByLabelText('editor') as HTMLTextAreaElement).readOnly).toBe(true)
    expect((screen.getByRole('button', { name: '从冻结源码创建 Revision 草稿' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
