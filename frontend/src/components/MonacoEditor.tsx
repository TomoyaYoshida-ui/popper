import { useEffect, useRef, useState } from 'react'
import * as monaco from 'monaco-editor/editor/editor.api.js'
// Keep the workbench editor small: no language-server UI, inline AI,
// code lenses, color pickers or other unused IDE contributions.
import 'monaco-editor/features/codeEditor/register.js'
import 'monaco-editor/features/diffEditor/register.js'
import 'monaco-editor/features/find/register.js'
import 'monaco-editor/features/tokenization/register.js'
import 'monaco-editor/features/bracketMatching/register.js'
import 'monaco-editor/features/clipboard/register.js'
import 'monaco-editor/features/cursorUndo/register.js'
import 'monaco-editor/features/folding/register.js'
import 'monaco-editor/features/indentation/register.js'
import 'monaco-editor/features/readOnlyMessage/register.js'
import 'monaco-editor/features/toggleTabFocusMode/register.js'
import 'monaco-editor/languages/definitions/python/register.js'
import 'monaco-editor/languages/definitions/javascript/register.js'
import 'monaco-editor/languages/definitions/typescript/register.js'
import 'monaco-editor/languages/definitions/markdown/register.js'
import 'monaco-editor/languages/definitions/shell/register.js'
import 'monaco-editor/languages/definitions/powershell/register.js'
import 'monaco-editor/languages/definitions/yaml/register.js'
import 'monaco-editor/languages/definitions/xml/register.js'
import 'monaco-editor/language/json/monaco.contribution.js'
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker'
import JsonWorker from 'monaco-editor/language/json/json.worker.js?worker'

(globalThis as typeof globalThis & { MonacoEnvironment: { getWorker: (_id: string, label: string) => Worker } }).MonacoEnvironment = { getWorker: (_id: string, label: string) => label === 'json' ? new JsonWorker() : new EditorWorker() }
function language(path: string) {
  const extension = path.split('.').pop()?.toLowerCase()
  return ({ py: 'python', js: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript', json: 'json', md: 'markdown', sh: 'shell', ps1: 'powershell', yaml: 'yaml', yml: 'yaml', xml: 'xml', tex: 'plaintext' } as Record<string, string>)[extension ?? ''] ?? 'plaintext'
}
function theme() { return ['light', 'bright'].includes(document.documentElement.getAttribute('data-theme') ?? '') ? 'vs' : 'vs-dark' }
export default function MonacoEditor({ value, onChange, path, readOnly = false, original, onSave }: {
  value: string; onChange?: (text: string) => void; path: string; readOnly?: boolean; original?: string; onSave?: () => void
}) {
  const host = useRef<HTMLDivElement>(null)
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | monaco.editor.IStandaloneDiffEditor | null>(null)
  const callbacks = useRef({ onChange, onSave })
  callbacks.current = { onChange, onSave }
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    if (!host.current) return
    const options: monaco.editor.IStandaloneEditorConstructionOptions = {
      theme: theme(), readOnly, automaticLayout: true, fontSize: 13, fontFamily: 'Cascadia Mono, Consolas, monospace',
      minimap: { enabled: false }, scrollBeyondLastLine: false, padding: { top: 12, bottom: 12 },
      wordWrap: language(path) === 'markdown' ? 'on' : 'off', tabSize: 2, renderLineHighlight: 'gutter',
      accessibilitySupport: 'auto', ariaLabel: `${original != null ? '差异查看' : '代码编辑'} ${path}`,
      // Stable textarea IME/accessibility path in embedded browsers. Native
      // EditContext is experimental and does not expose a normal editable node.
      editContext: false,
    }
    const model = monaco.editor.createModel(value, language(path))
    let originalModel: monaco.editor.ITextModel | null = null
    let editor: monaco.editor.IStandaloneCodeEditor | monaco.editor.IStandaloneDiffEditor
    if (original != null) {
      originalModel = monaco.editor.createModel(original, language(path))
      const diff = monaco.editor.createDiffEditor(host.current, { ...options, renderSideBySide: false, originalEditable: false })
      diff.setModel({ original: originalModel, modified: model }); editor = diff
    } else editor = monaco.editor.create(host.current, { ...options, model })
    editorRef.current = editor
    const subscription = model.onDidChangeContent(() => callbacks.current.onChange?.(model.getValue()))
    const code = 'getModifiedEditor' in editor ? editor.getModifiedEditor() : editor
    code.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => callbacks.current.onSave?.())
    const observer = new MutationObserver(() => monaco.editor.setTheme(theme()))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    setLoaded(true)
    return () => { subscription.dispose(); observer.disconnect(); editor.dispose(); model.dispose(); originalModel?.dispose(); editorRef.current = null }
    // Models are local to each tab. Controlled content is synchronized separately without losing undo history.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, original != null])
  useEffect(() => {
    const editor = editorRef.current
    if (!editor) return
    const code = 'getModifiedEditor' in editor ? editor.getModifiedEditor() : editor
    if (code.getValue() !== value) code.setValue(value)
    if ('getOriginalEditor' in editor && original != null && editor.getOriginalEditor().getValue() !== original) editor.getOriginalEditor().setValue(original)
    editor.updateOptions({ readOnly })
  }, [value, original, readOnly, loaded])
  return <div className="monaco-host" ref={host} />
}
