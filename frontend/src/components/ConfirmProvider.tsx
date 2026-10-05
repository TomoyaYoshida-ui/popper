import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'

export type Confirmation = { title: string; description: string; details?: Array<{ label: string; value: string }>; confirmLabel?: string; danger?: boolean }
type Confirm = (options: Confirmation) => Promise<boolean>
const Context = createContext<Confirm>(async () => false)
export const useConfirmation = () => useContext(Context)

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [prompt, setPrompt] = useState<Confirmation | null>(null)
  const pending = useRef<((ok: boolean) => void) | null>(null)
  const previousFocus = useRef<HTMLElement | null>(null)
  const dialog = useRef<HTMLElement>(null)
  const finish = useCallback((ok: boolean) => {
    const resolve = pending.current
    pending.current = null; setPrompt(null); resolve?.(ok)
    if (previousFocus.current?.isConnected) previousFocus.current.focus()
  }, [])
  const confirm = useCallback<Confirm>((options) => {
    if (pending.current) return Promise.resolve(false)
    previousFocus.current = document.activeElement as HTMLElement | null
    return new Promise(resolve => { pending.current = resolve; setPrompt(options) })
  }, [])
  useEffect(() => () => { pending.current?.(false); pending.current = null }, [])
  useEffect(() => {
    if (!prompt) return
    dialog.current?.querySelector<HTMLButtonElement>('button')?.focus()
    const keys = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); finish(false) }
      if (event.key !== 'Tab') return
      const buttons = [...(dialog.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])]
      const first = buttons[0], last = buttons[buttons.length - 1]
      if (event.shiftKey && (document.activeElement === first || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && (document.activeElement === last || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', keys, true)
    return () => document.removeEventListener('keydown', keys, true)
  }, [prompt, finish])
  return <Context.Provider value={confirm}>{children}{prompt && <div className="wb-dialog-backdrop">
    <section ref={dialog} className="wb-dialog action-confirmation" role="dialog" aria-modal="true" aria-labelledby="action-confirm-title" aria-describedby="action-confirm-description">
      <h2 id="action-confirm-title">{prompt.title}</h2><p id="action-confirm-description">{prompt.description}</p>
      {!!prompt.details?.length && <dl className="resource-facts">{prompt.details.map((item, index) => <div key={index}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl>}
      <div className="dialog-actions"><button type="button" onClick={() => finish(false)}>取消</button>
        <button type="button" className={prompt.danger ? 'danger' : 'primary'} onClick={() => finish(true)}>{prompt.confirmLabel ?? '确认执行'}</button></div>
    </section></div>}</Context.Provider>
}
