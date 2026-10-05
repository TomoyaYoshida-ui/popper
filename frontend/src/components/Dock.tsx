/** 右侧悬浮 dock（对齐设计稿）。onActivate(id) 由 App 分派到对应视图/面板。 */
const ITEMS: { id: string; label: string; icon: string }[] = [
  { id: 'lit', label: '知识库', icon: '▤' },
  { id: 'terminal', label: 'Popper 终端', icon: '>' },
  { id: 'products', label: '产物', icon: '⇓' },
]

export function Dock({ onActivate, terminalCapability }: {
  onActivate: (id: string) => void
  terminalCapability: { available: boolean; reason: string | null } | null
}) {
  return (
    <div className="dock">
      {ITEMS.map((it) => (
        <button key={it.id} className="di"
          title={it.id === 'terminal' && !terminalCapability?.available
            ? `${it.label}：${terminalCapability?.reason ?? '正在检查终端能力'}` : it.label}
          disabled={it.id === 'terminal' && !terminalCapability?.available}
          onClick={() => onActivate(it.id)}>
          {it.icon}
        </button>
      ))}
    </div>
  )
}
