import { useEffect, useRef, useState } from 'react'
import { WorkbenchIcon } from './WorkbenchIcon'

const THEMES = [
  { key: 'warm', label: '暖黑' },
  { key: 'cool', label: '冷黑' },
  { key: 'bright', label: '纯白' },
  { key: 'light', label: '象牙白' },
]

function applyTheme(key: string) {
  if (key === 'warm') document.documentElement.removeAttribute('data-theme')
  else document.documentElement.setAttribute('data-theme', key)
  try { localStorage.setItem('theme', key) } catch { /* Theme still works without storage. */ }
}

/** Only expose the theme setting that the workbench actually implements. */
export function SettingsMenu({ placement = 'top' }: { placement?: 'top' | 'rail' }) {
  const [open, setOpen] = useState(false)
  const [theme, setTheme] = useState(() => {
    let saved: string | null = null
    try { saved = localStorage.getItem('theme') } catch { /* Use the default theme. */ }
    return THEMES.some((item) => item.key === saved) ? saved! : 'bright'
  })
  const boxRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => { applyTheme(theme) }, [theme])
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])
  useEffect(() => {
    if (!open) return
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setOpen(false); triggerRef.current?.focus() }
    }
    document.addEventListener('keydown', close)
    return () => document.removeEventListener('keydown', close)
  }, [open])

  const pickTheme = (key: string) => {
    setTheme(key)
    setOpen(false)
  }

  return (
    <div ref={boxRef} style={{ position: 'relative' }}>
      <button ref={triggerRef} className="gear" type="button" title="设置主题" aria-label="设置主题" aria-expanded={open}
        onClick={() => setOpen((v) => !v)}><WorkbenchIcon kind="settings" /></button>
      {open && (
        <div className="ctx-menu popout settings" role="group" aria-label="主题选择" style={placement === 'rail' ? { left: 'calc(100% + 10px)', bottom: 0 } : { right: 0, top: 'calc(100% + 6px)' }}>
          <div className="ctx-item" style={{ pointerEvents: 'none', color: 'var(--faint)', fontSize: 11 }}>
            主题 <span style={{ marginLeft: 'auto' }}>{THEMES.find((t) => t.key === theme)?.label}</span>
          </div>
          {THEMES.map((t) => (
            <button key={t.key} type="button" className="ctx-item" aria-pressed={theme === t.key} onClick={() => pickTheme(t.key)}>
              <span className={`theme-swatch ${t.key}`} aria-hidden="true" />{t.label} {theme === t.key ? <span className="ok" style={{ marginLeft: 'auto' }}>✓</span> : null}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
