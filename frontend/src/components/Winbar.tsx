/** Browser workbench header: only controls backed by a real action. */
export function Winbar({ onOpenEditor }: { onOpenEditor: () => void }) {
  return (
    <header className="winbar">
      <div className="win-title">
        <span className="win-brand">POPPER</span>
        <span className="win-div">·</span>
        <span className="win-env">科研工作台</span>
      </div>

      <div className="rgt">
        <button className="open-ed" type="button" onClick={onOpenEditor}>
          <span className="qlogo">P</span>打开编辑器
        </button>
        <span className="win-host">{window.location.host}</span>
      </div>
    </header>
  )
}
