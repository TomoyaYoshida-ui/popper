import type { Workspace } from '../types'

export function TaskHistory({ workspace }: { workspace: Workspace | null }) {
  return <section className="task-history"><header className="panel-heading"><div><span className="eyebrow">PROJECT HISTORY</span><h2>历史任务</h2></div><span className="resource-status">只读</span></header>
    <p className="muted">旧 Quest 的项目级记录，不是 Agent 会话，也不代表 Study 的实验或待审批操作。</p>
    {!workspace ? <p className="panel-loading">正在读取历史任务…</p> : !workspace.folders.length ? <p className="panel-empty">这个项目没有历史任务。</p>
      : workspace.folders.map(folder => <section className="history-folder" key={folder.fid}><h3>{folder.name}</h3>
        {!folder.tasks.length && <p className="muted">没有任务记录。</p>}
        <ul>{folder.tasks.map(task => <li key={task.quid}><span className="resource-status">{task.done ? '已完成' : '未完成'}</span><strong>{task.title}</strong><time dateTime={task.created}>{Number.isNaN(Date.parse(task.created)) ? task.created : new Date(task.created).toLocaleString('zh-CN')}</time></li>)}</ul>
      </section>)}
  </section>
}
