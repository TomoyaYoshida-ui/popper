import { useCallback, useEffect, useRef, useState } from 'react'
import { readDraft, writeDraft, type EditorDraft } from './workbench'

const queues = new Map<string, Promise<void>>()
function persist(value: EditorDraft | string): Promise<void> {
  const key = typeof value === 'string' ? value : value.key
  const prior = queues.get(key) ?? Promise.resolve()
  const next = prior.catch(() => undefined).then(() => writeDraft(value))
  queues.set(key, next)
  void next.finally(() => { if (queues.get(key) === next) queues.delete(key) }).catch(() => undefined)
  return next
}
export async function restoreEditorDraft(key: string): Promise<EditorDraft | null> {
  await queues.get(key)?.catch(() => undefined)
  return readDraft(key)
}
export function useEditorDraft(key: string | null, content: string, baseContent: string, baseSha256: string | null, dirty: boolean, revisionMode = false) {
  const latest = useRef(new Map<string, EditorDraft>())
  const [storageError, setStorageError] = useState<string | null>(null)
  if (key && baseSha256 && dirty) latest.current.set(key, { key, content, baseContent, baseSha256, updated: Date.now(), revisionMode })
  else if (key) latest.current.delete(key)
  useEffect(() => {
    if (!key || !baseSha256) return
    const snapshot = latest.current.get(key)
    const timer = window.setTimeout(() => {
      void persist(snapshot ?? key).catch(() => setStorageError('本地草稿保存失败。请保持页面打开并保存文件。'))
    }, 350)
    return () => window.clearTimeout(timer)
  }, [key, content, baseContent, baseSha256, dirty, revisionMode])
  useEffect(() => () => {
    if (!key) return
    const snapshot = latest.current.get(key)
    if (snapshot) void persist(snapshot).catch(() => undefined)
  }, [key])
  const clearDraft = useCallback(async () => {
    if (!key) return
    latest.current.delete(key)
    await persist(key).catch(() => setStorageError('文件已保存，但本地草稿清理失败。'))
  }, [key])
  return { storageError, clearDraft }
}
