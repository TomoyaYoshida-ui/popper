import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { defaultLayout, openStudyResource, openTab, readDraft, removeTab, resourceTab, restoreLayout, writeDraft } from '../src/workbench'
import { restoreEditorDraft, useEditorDraft } from '../src/useEditorDraft'

describe('workbench layout', () => {
  it('restores explorer visibility without invalidating old layouts or file tabs', () => {
    const old = openTab(defaultLayout(), { kind: 'files', title: '项目文件' })
    const restored = restoreLayout(JSON.stringify(old), 1440)
    expect(restored.tabs.find((tab) => tab.kind === 'files')?.title).toBe('打开文件')
    expect(restored.activeTab).toBe(old.activeTab)
    expect(restored.fileExplorerOpen).toBe(true)
    expect(restoreLayout(JSON.stringify({ ...old, fileExplorerOpen: false }), 1440).fileExplorerOpen).toBe(false)
  })
  it('binds the placeholder Study once and reuses it from brand or object links', () => {
    const first = openStudyResource(defaultLayout(), { kind: 'study', studyId: 'S-A' })
    expect(first.tabs).toHaveLength(1)
    expect(first.tabs[0].studyId).toBe('S-A')
    expect(openStudyResource(first, { kind: 'study' }, 'S-A').tabs).toHaveLength(1)
    const second = openStudyResource(first, { kind: 'study', studyId: 'S-B' })
    expect(second.tabs).toHaveLength(2)
    expect(openStudyResource(second, { kind: 'study', studyId: 'S-B' }).tabs).toHaveLength(2)
  })
  it('deduplicates the same resource and distinguishes study ownership', () => {
    const first = openTab(defaultLayout(), { kind: 'claim', ref: 'C001', studyId: 'A' })
    const repeated = openTab(first, { kind: 'claim', ref: 'C001', studyId: 'A' })
    expect(repeated.tabs).toHaveLength(2)
    expect(repeated.activeTab).toBe(first.activeTab)
    expect(openTab(repeated, { kind: 'claim', ref: 'C001', studyId: 'B' }).tabs).toHaveLength(3)
  })
  it('closes the active tab to its neighbour and permits an empty workspace', () => {
    const layout = openTab(defaultLayout(), { kind: 'file', ref: 'model.py' })
    const closed = removeTab(layout, layout.activeTab!)
    expect(closed.activeTab).toBe(closed.tabs[0].id)
    expect(removeTab(closed, closed.activeTab!).activeTab).toBeNull()
    expect(restoreLayout(JSON.stringify({ ...closed, tabs: [] }), 1440).tabs).toEqual([])
  })
  it('rejects incompatible or malformed stored state and clamps dimensions', () => {
    expect(restoreLayout('{broken', 1024).sidebarOpen).toBe(false)
    expect(restoreLayout(JSON.stringify({ version: 2, tabs: [] }), 1440).tabs).toHaveLength(1)
    const tab = resourceTab({ kind: 'file', ref: 'model.py' })
    const restored = restoreLayout(JSON.stringify({ ...defaultLayout(), tabs: [tab, tab, { kind: 'invalid' }], activeTab: 'missing', chatWidth: 9000, sidebarWidth: -10, bottomHeight: 'bad' }), 1440)
    expect(restored.tabs).toHaveLength(1)
    expect(restored.activeTab).toBe(tab.id)
    expect(restored.chatWidth).toBe(720)
    expect(restored.sidebarWidth).toBe(200)
    expect(restored.bottomHeight).toBe(260)
  })
})
describe('editor recovery', () => {
  it('retains content and its base version in IndexedDB', async () => {
    const draft = { key: 'draft:one', content: 'changed', baseContent: 'original', baseSha256: 'abc', updated: 1 }
    await writeDraft(draft)
    expect(await readDraft(draft.key)).toEqual(draft)
    await writeDraft(draft.key)
    expect(await readDraft(draft.key)).toBeNull()
  })
  it('flushes a last-moment edit on unmount before the next editor restores it', async () => {
    const { unmount } = renderHook(() => useEditorDraft('draft:switch', 'unsaved', 'base', 'hash', true))
    unmount()
    expect((await restoreEditorDraft('draft:switch'))?.content).toBe('unsaved')
  })
  it('does not resurrect a discarded draft when its tab closes', async () => {
    const { result, unmount } = renderHook(() => useEditorDraft('draft:discard', 'unsaved', 'base', 'hash', true))
    await act(async () => { await result.current.clearDraft() })
    unmount()
    expect(await restoreEditorDraft('draft:discard')).toBeNull()
  })
})
