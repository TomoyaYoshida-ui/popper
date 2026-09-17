/** 右侧悬浮 dock（对齐设计稿）。onActivate(id) 由 App 分派到对应视图/面板。 */
const ITEMS: { id: string; label: string; icon: string }[] = [
  { id: 'lit', label: '知识库', icon: '▤' },
  { id: 'terminal', label: '终端', icon: '>' },
  { id: 'products', label: '产物', icon: '⇓' },
  { id: 'docs', label: '文档', icon: '▤' },
  { id: 'browser', label: '浏览器', icon: '◉' },
]

export function Dock({ onActivate }: { onActivate: (id: string) => void }) {
  return (
    <div className="dock">
      {ITEMS.map((it) => (
        <button key={it.id} className="di" title={it.label} onClick={() => onActivate(it.id)}>
          {it.icon}
        </button>
      ))}
    </div>
  )
}