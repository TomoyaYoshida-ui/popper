/** 40px 窗口顶栏（对齐融合设计稿 winbar：面板/搜索 + 品牌 + 编辑器 + 窗口控制）。 */
export function Winbar() {
  return (
    <header className="winbar">
      <div className="lft">
        <button className="wbtn" title="侧栏面板">
          <svg className="i" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2.5" /><path d="M9.5 4v16" /></svg>
        </button>
        <button className="wbtn" title="搜索">
          <svg className="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
        </button>
      </div>

      <div className="win-title">
        <span className="win-brand">POPPER</span>
        <span className="win-div">·</span>
        <span className="win-env">Quest on, hands off</span>
      </div>

      <div className="rgt">
        <button className="open-ed">
          <span className="qlogo">P</span>打开编辑器
          <svg className="i" style={{ width: 11, height: 11 }} viewBox="0 0 24 24"><path d="m6 9 6 6 6-6" /></svg>
        </button>
        <span className="win-host">127.0.0.1</span>
        <button className="wbtn" title="更多">⋯</button>
        <div className="winctl">
          <button className="wbtn">—</button>
          <button className="wbtn">□</button>
          <button className="wbtn">✕</button>
        </div>
      </div>
    </header>
  )
}