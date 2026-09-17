import { useEffect, useRef, useState } from 'react'

const THEMES = [
  { key: 'warm', label: '暖黑' },
  { key: 'cool', label: '冷黑' },
  { key: 'bright', label: '纯白' },
  { key: 'light', label: '象牙白' },
]
const LANGS = ['简体中文', 'English']

function applyTheme(key: string) {
  if (key === 'warm') document.documentElement.removeAttribute('data-theme')
  else document.documentElement.setAttribute('data-theme', key)
  localStorage.setItem('theme', key)
}

/** 齿轮弹出的设置菜单：主题 / 界面语言，写 localStorage 并即时生效。 */
export function SettingsMenu() {
  const [open, setOpen] = useState(false)
  const [theme, setTheme] = useState(() => (localStorage.getItem('theme') || 'warm') as string)
  const [lang, setLang] = useState(() => localStorage.getItem('lang') || '简体中文')
  const boxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  const pickTheme = (key: string) => {
    setTheme(key)
    applyTheme(key)
  }
  const pickLang = (l: string) => {
    setLang(l)
    localStorage.setItem('lang', l)
  }

  return (
    <div ref={boxRef} style={{ position: 'relative' }}>
      <span className="gear" title="设置" onClick={() => setOpen((v) => !v)}>⚙</span>
      {open && (
        <div className="ctx-menu popout settings" style={{ right: 0, top: 'calc(100% + 6px)' }}>
          <div className="ctx-item" style={{ pointerEvents: 'none', color: 'var(--faint)', fontSize: 11 }}>
            界面语言 <span style={{ marginLeft: 'auto' }}>{lang}</span>
          </div>
          {LANGS.map((l) => (
            <div key={l} className="ctx-item" onClick={() => pickLang(l)}>
              {l} {lang === l ? <span className="ok" style={{ marginLeft: 'auto' }}>✓</span> : null}
            </div>
          ))}
          <div className="ctx-sep" />
          <div className="ctx-item" style={{ pointerEvents: 'none', color: 'var(--faint)', fontSize: 11 }}>
            主题 <span style={{ marginLeft: 'auto' }}>{THEMES.find((t) => t.key === theme)?.label}</span>
          </div>
          {THEMES.map((t) => (
            <div key={t.key} className="ctx-item" onClick={() => pickTheme(t.key)}>
              {t.label} {theme === t.key ? <span className="ok" style={{ marginLeft: 'auto' }}>✓</span> : null}
            </div>
          ))}
          <div className="ctx-sep" />
          <div className="ctx-item danger" onClick={() => setOpen(false)}>
            退出登录
          </div>
        </div>
      )}
    </div>
  )
}