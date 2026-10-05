import type { ChatState } from '../types'
import { toolCallDiff } from '../toolCallDiff'

export function ToolDiffPanel({ call, studyId }: { call: ChatState['tool_calls'][number] | undefined; studyId?: string }) {
  const diff = toolCallDiff(call)
  const readable = diff && (!studyId || diff.studyId === studyId)
  return <div className="work-page"><h2>源码修改差异</h2>{readable ? <>
    {diff.path && <p className="muted">{diff.path}</p>}
    {diff.source === 'completed_preview_result' && <p className="notice">历史预览回执 · 仅预览源码差异，没有创建 Revision 或执行实验。创建和执行仍需分别审批；此记录不能替代当前审批。</p>}
    <pre className="source-diff">{diff.diff}</pre>
    {['pending_approval', 'approved'].includes(call!.status) && <p className="muted">请回到对话中的工具卡片批准或拒绝此修改。</p>}
    <details className="resource-technical"><summary>差异来源与预览版本</summary><dl className="resource-facts">
      <div><dt>来源工具调用</dt><dd><code>{diff.callId}</code></dd></div>
      {diff.studyId && <div><dt>所属 Study</dt><dd><code>{diff.studyId}</code></dd></div>}
      {diff.stateVersion != null && <div><dt>预览状态版本</dt><dd>{diff.stateVersion}</dd></div>}
      {diff.proposalSha256 && <div><dt>原提案摘要</dt><dd><code>{diff.proposalSha256}</code></dd></div>}
    </dl></details>
  </> : <div className="notice">此差异当前不可读取，请打开对应会话；不会根据工具参数重建或猜测源码差异。</div>}</div>
}
