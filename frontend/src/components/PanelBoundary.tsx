import { Component, type ReactNode } from 'react'

/** An unloaded editor or broken resource must not take down the other panels. */
export class PanelBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    if (this.state.failed) return <div className="panel-failure" role="alert">
      <h2>此面板暂时无法加载</h2>
      <p>可能是工作台刚完成更新或资源连接中断。其他面板仍可使用；已保存的本地草稿会在刷新后恢复。</p>
      <button type="button" onClick={() => window.location.reload()}>重新载入工作台</button>
    </div>
    return this.props.children
  }
}
