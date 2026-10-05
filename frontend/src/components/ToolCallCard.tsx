import type { ChatState } from '../types'
import { useRef, useState } from 'react'
import type { WorkbenchResource } from '../workbench'
import { DevelopmentDiagnostics } from './DevelopmentDiagnostics'
import { toolCallDiff } from '../toolCallDiff'

type ToolCall = ChatState['tool_calls'][number]
const TOOL_LABELS: Record<string, string> = {
  'context.source': '读取已确认交接来源',
  'research.status': '查看研究状态', 'research.study_preview': '预览研究执行', 'research.study_action': '推进 Study',
  'research.study_job': '查看研究作业', 'research.create_hypothesis': '提出研究假设', 'research.create_design': '创建实验设计',
  'research.freeze_design': '冻结实验设计', 'research.execute_design': '执行实验设计', 'research.inspect_design': '查看实验设计',
  'research.inspect_claim': '核验 Claim', 'research.inspect_observation': '查看 Observation',
  'research.draft_preview': '预览证据绑定稿件', 'research.draft_create': '生成证据绑定稿件',
  'research.revision_preview': '预览源码 Revision', 'research.revision_create': '创建不可变 Revision',
  'research.revision_execute': '执行源码 Revision', 'research.revision_job': '查看 Revision 作业',
  'research.revision_commit': '创建不可变 Revision', 'research.revision_run': '执行源码 Revision',
  'workspace.file_list': '浏览项目文件', 'workspace.file_read': '读取项目文件', 'workspace.file_replace': '修改项目文件',
}
const STATUS_LABELS: Record<ToolCall['status'], string> = {
  pending_approval: '等待审批', approved: '已批准', rejected: '已拒绝', running: '执行中', completed: '已完成',
  failed: '失败', cancelled: '已取消', interrupted: '已中断',
  invalid: '参数无效，未执行',
}
function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function executionQueued(call: ToolCall): boolean {
  if (call.status !== 'completed' || !['research.revision_execute', 'research.revision_run', 'research.execute_design', 'research.study_action'].includes(call.tool_name)) return false
  const result = object(call.result)
  const data = { ...result, ...object(result.data) }
  return data.status === 'enqueued' || ['queued', 'enqueued', 'running'].includes(String(object(data.job).status ?? ''))
}
function ToolSummary({ call, projectName, onOpenResource }: { call: ToolCall; projectName?: string; onOpenResource: (resource: WorkbenchResource) => void }) {
  if (call.status === 'invalid') return projectName ? <p className="tool-context">所属项目 · {projectName}</p> : null
  const result = object(call.result)
  const data = { ...result, ...object(result.data) }
  const job = object(data.job)
  const diff = toolCallDiff(call)
  const plan = object(call.preview?.study_action ?? call.preview?.bound_draft ?? call.preview?.revision_preview)
  const version = plan.state_version ?? plan.expected_state_version ?? call.arguments.expected_version ?? call.preview?.state_version
  const file = object(call.status === 'completed' && call.tool_name === 'workspace.file_read'
    ? data : call.preview?.file_read ?? call.preview?.file_edit)
  const studyId = [call.arguments.study_id, plan.study_id, data.study_id, object(job.request).study_id].find((value) => typeof value === 'string') as string | undefined
  const resources: WorkbenchResource[] = []
  if (studyId) resources.push({ kind: 'study', studyId, title: 'Study' })
  const path = call.preview?.file_edit?.path ?? call.arguments.path
  if (typeof path === 'string') resources.push({ kind: 'file', ref: path })
  if (diff) resources.push({ kind: 'diff', ref: call.call_id, ...(diff.studyId ? { studyId: diff.studyId } : {}), title: '源码差异' })
  for (const [key, kind] of [['claim_id', 'claim'], ['observation_id', 'observation'], ['job_id', 'job'], ['draft_id', 'manuscript']] as const) {
    const value = call.arguments[key] ?? data[key] ?? job[key]
    if (typeof value === 'string') resources.push({ kind, ref: value, studyId,
      title: { claim: '研究结论', observation: '实验观察', job: '运行作业', manuscript: '证据绑定稿件' }[kind] })
  }
  const observations = Array.isArray(data.observations) ? data.observations.map(object) : []
  for (const observation of observations) {
    if (typeof observation.observation_id === 'string') resources.push({ kind: 'observation',
      ref: observation.observation_id, studyId, title: '实验观察' })
  }
  return <>
    {projectName && <p className="tool-context">所属项目 · {projectName}{studyId ? ' / Study' : ''}</p>}
    {(version != null || file.path) && <dl className="tool-summary">
      {version != null && <div><dt>审批版本</dt><dd>v{String(version)}{call.preview?.version_matches === false ? ' · 已变化，必须重新预览' : ''}</dd></div>}
      {file.path != null && <div><dt>影响范围</dt><dd>{String(file.path)}</dd></div>}
      {file.bytes != null && <div><dt>披露正文</dt><dd>{String(file.bytes)} bytes · 仅审批后发送给模型</dd></div>}
    </dl>}
    {(file.sha256 ?? file.before_sha256) != null && <details className="tool-technical"><summary>输入版本与校验摘要</summary>
      <p>{String(file.sha256 ?? file.before_sha256)}</p><button type="button" onClick={() => { void navigator.clipboard?.writeText(String(file.sha256 ?? file.before_sha256)) }}>复制摘要</button></details>}
    {Object.keys(plan).length > 0 && <dl className="tool-summary">
      {['objective', 'scope', 'selected_hypothesis_id', 'confirmation_cost', 'max_steps'].map((key) => plan[key] != null && <div key={key}>
        <dt>{{ objective: '研究目标', scope: '执行范围', selected_hypothesis_id: '选定候选', confirmation_cost: '确认成本', max_steps: '最多步数' }[key]}</dt>
        <dd>{String(plan[key])}</dd>
      </div>)}
      {object(plan.budget).available != null && <div><dt>可用预算</dt><dd>{String(object(plan.budget).available)}</dd></div>}
      {Array.isArray(plan.stop_conditions) && <div><dt>停止条件</dt><dd>{plan.stop_conditions.map(String).join('、')}</dd></div>}
      {plan.action === 'confirm' && <div className="confirmation-caution"><dt>独立确认</dt><dd>会消费一次性确认资源，消费后不能自动重试。</dd></div>}
    </dl>}
    {!!resources.length && <div className="resource-links">{resources.map((resource, index) => <button key={index} type="button"
      title={resource.ref ?? resource.studyId} onClick={() => onOpenResource(resource)}>↗ {resource.title ?? resource.ref ?? 'Study'}</button>)}</div>}
    {!!observations.length && <dl className="tool-summary">{observations.map((observation, index) => <div key={index}>
      <dt>{String(observation.metric ?? '开发观察')}</dt><dd>{String(observation.value)} · 仅开发集</dd></div>)}</dl>}
    {call.status === 'completed' && data.development_diagnostics != null && <DevelopmentDiagnostics value={data.development_diagnostics} studyId={studyId}
      jobId={typeof job.job_id === 'string' ? job.job_id : undefined} compact onOpenResource={onOpenResource} />}
    {call.status === 'completed' && data.development_diagnostics_error != null && <p className="chat-error" role="status">开发诊断不可读取：{typeof data.development_diagnostics_error === 'string' ? data.development_diagnostics_error : String(object(data.development_diagnostics_error).message ?? '诊断核验失败')}。已有作业记录仍可查看，未补造诊断或成绩。</p>}
    {studyId && <details className="tool-technical"><summary>对象标识</summary><code>{studyId}</code>
      <button type="button" onClick={() => { void navigator.clipboard?.writeText(studyId) }}>复制 Study ID</button></details>}
  </>
}
export function ToolCallCard({ call, projectName, chatPending, onToolAction, onOpenResource }: {
  call: ToolCall; chatPending: boolean
  projectName?: string
  onToolAction: (callId: string, action: 'approve' | 'execute' | 'reject' | 'cancel') => Promise<void>
  onOpenResource: (resource: WorkbenchResource) => void
}) {
  const actionInFlight = useRef(false)
  const [localPending, setLocalPending] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const blocked = chatPending || localPending
  const createsRevision = ['research.revision_create', 'research.revision_commit'].includes(call.tool_name)
  const runsRevision = ['research.revision_execute', 'research.revision_run'].includes(call.tool_name)
  const queued = executionQueued(call)
  const diff = toolCallDiff(call)
  const revisionApprovalLabel = createsRevision
    ? '批准创建 Revision（不执行）' : runsRevision
      ? '批准排队执行 Revision' : undefined
  const invalidApproval = (call.tool_name.startsWith('research.revision_') || ['research.create_design', 'research.create_hypothesis', 'research.status', 'research.freeze_design', 'research.inspect_design', 'research.inspect_claim', 'research.inspect_observation', 'research.execute_design', 'research.study_preview', 'research.study_action', 'research.study_job', 'research.draft_preview', 'research.draft_create', 'workspace.file_list', 'workspace.file_read', 'workspace.file_replace'].includes(call.tool_name))
    && (!call.preview?.ok || call.preview.version_matches === false
      || (call.tool_name === 'research.create_design' && call.preview.hypothesis_version_matches === false)
      || (call.tool_name === 'research.execute_design' && call.preview.design_sha256_matches === false)
      || (call.tool_name === 'research.study_action' && call.preview.approval_matches === false)
      || (call.tool_name.startsWith('research.revision_') && call.preview.approval_matches === false))
  const act = async (action: 'approve' | 'execute' | 'reject' | 'cancel') => {
    if (blocked || actionInFlight.current || call.status === 'invalid' || (['approve', 'execute'].includes(action) && invalidApproval)) return
    actionInFlight.current = true
    setLocalPending(true); setActionError(null)
    try { await onToolAction(call.call_id, action) }
    catch (cause) { setActionError((cause as Error).message) }
    finally { actionInFlight.current = false; setLocalPending(false) }
  }
  return (
            <div className={`tool-call-card ${call.status}`} >
              <div className="tool-call-head">
                <span>{TOOL_LABELS[call.tool_name] ?? call.tool_name}</span>
                <b>{queued ? '已入队，等待作业结果' : call.status === 'pending_approval' && createsRevision ? '等待创建审批' : call.status === 'pending_approval' && runsRevision ? '等待执行审批' : STATUS_LABELS[call.status]}</b>
              </div>
              <ToolSummary call={call} projectName={projectName} onOpenResource={onOpenResource} />
              <details className="tool-technical"><summary>查看工具参数</summary>{call.status === 'invalid' && (call.original_tool_name ?? call.validation_error?.original_tool_name) && <p>原工具名：{call.original_tool_name ?? call.validation_error?.original_tool_name}</p>}<pre>{JSON.stringify(call.status === 'invalid' ? call.raw_arguments ?? call.validation_error?.raw_arguments ?? call.arguments : call.arguments, null, 2)}</pre></details>
              {call.status === 'invalid' && <div className="tool-call-reason">参数校验未通过，工具没有执行，不需要批准或执行。仅在用量已核实且允许续推理时，Agent 才会根据错误说明纠正提案。</div>}
              {queued && <div className="tool-call-reason" role="status">执行请求已登记，实验尚未完成。请查看关联 Job 的真实终态与 Observation，入队不代表实验成功。</div>}
              {call.tool_name === 'research.freeze_design' && <div className="tool-call-reason">冻结实验设计是不可逆的科研状态变更；请核对下方权威设计内容和版本，再决定是否批准。</div>}
              {call.tool_name === 'research.create_hypothesis' && <div className="tool-call-reason">此操作会向研究库写入一项未测试假设，不构成实验结果或结论。请核对研究问题、适用范围、预测、证伪条件和替代解释。</div>}
              {call.tool_name === 'research.create_design' && <div className="tool-call-reason">此操作只创建实验设计草稿；尚未绑定领域执行配置和不可变源码，不能据此声称可运行或已有实验结果。</div>}
              {call.tool_name === 'research.execute_design' && <div className="tool-call-reason">批准后将冻结设计送入沙箱执行队列，开发预算会被消耗，失败也可能计费。工具结果只代表 Job 已登记，不代表实验成功或形成结论。</div>}
              {call.tool_name === 'research.study_action' && <div className="tool-call-reason">核对研究目标、当前预算、停止条件和选定候选。独立确认会消费一次性确认资源；作业入队不代表实验成功。</div>}
              {call.tool_name === 'research.draft_create' && <div className="tool-call-reason">核对 Study 版本、全部 Claim、标题和作者说明。结果仅是待人工审校的证据绑定草稿；作者说明不经系统验证。</div>}
              {createsRevision && <div className="tool-call-reason">创建 Agent 作者的不可变源码版本，不覆盖冻结文件、不执行实验。请核对差异；执行需要另一次审批。{typeof call.arguments.parent_revision_id === 'string' && '本次修复绑定已失败的父版本，失败记录及其预算消耗保持不变。'}{call.tool_name === 'research.revision_commit' && '源码与审批版本由服务端绑定到已完成预览，预览回执不等于创建批准。'}</div>}
              {runsRevision && <div className="tool-call-reason">消耗开发预算，失败同样计入。此工具完成只表示 Job 已入队，请查看真实作业终态和 Observation。{call.tool_name === 'research.revision_run' && '执行引用已创建的不可变 Revision；创建批准不等于执行批准。'}</div>}
              {diff?.kind === 'revision' && <details open={call.status === 'pending_approval'}><summary>Revision 源码差异{diff.source === 'completed_preview_result' ? ' · 历史预览回执' : ''}</summary>
                {diff.source === 'completed_preview_result' && <p className="muted">仅完成预览，不代表已创建 Revision 或执行实验；创建与执行需要分别审批。</p>}<pre>{diff.diff}</pre>
                {diff.source === 'completed_preview_result' && <details className="tool-technical"><summary>原预览来源与提案摘要</summary><p>来源工具调用：{diff.callId}</p>{diff.stateVersion != null && <p>预览状态版本：{diff.stateVersion}</p>}{diff.proposalSha256 && <code>{diff.proposalSha256}</code>}</details>}
              </details>}
              {call.tool_name === 'workspace.file_replace' && <div className="tool-call-reason">这会修改当前工作区的真实源码。请审查下方服务端生成的差异；冻结实验输入不可原地修改，外部修改会触发版本冲突。</div>}
              {call.preview && <details open={call.status === 'pending_approval' && !!call.preview.file_edit}>
                <summary>{call.tool_name === 'research.study_action' ? 'Study 执行范围与审批摘要' : call.tool_name === 'research.study_preview' ? '将向模型披露的研究方案' : call.tool_name === 'research.study_job' ? '将向模型披露的作业结果' : call.tool_name === 'research.draft_create' ? '证据绑定稿件 · 审批前必读' : call.tool_name === 'research.draft_preview' ? '将向模型披露的稿件方案' : call.tool_name === 'workspace.file_replace' ? '源码补丁差异 · 审批前必读' : call.tool_name === 'workspace.file_read' ? '将向模型披露的源码正文' : call.tool_name === 'workspace.file_list' ? '将向模型披露的文件列表' : call.tool_name === 'research.create_design' ? '设计草稿提案与目标假设' : call.tool_name === 'research.create_hypothesis' ? '假设提案与所属研究' : call.tool_name === 'research.status' ? '将向模型披露的 Study 状态摘要' : call.tool_name === 'research.inspect_design' ? '将向模型披露的设计与假设' : call.tool_name === 'research.inspect_claim' ? '将向模型披露的 Claim 与证据' : call.tool_name === 'research.inspect_observation' ? '将向模型披露的 Observation 与来源 Run' : '研究库中的设计预览'}{call.preview.version_matches === false ? ' · 研究版本已变化' : ''}{call.tool_name === 'research.create_design' && call.preview.hypothesis_version_matches === false ? ' · 假设已有新版本' : ''}</summary>
                {call.preview.ok ? call.preview.file_edit ? <pre>{call.preview.file_edit.diff}</pre> : <pre>{JSON.stringify(call.preview.file_list ?? call.preview.file_read ?? call.preview.research_status ?? call.preview.study_action ?? call.preview.bound_draft ?? call.preview.job ?? call.preview.observation ?? call.preview.binding ?? call.preview.proposal ?? call.preview.design, null, 2)}</pre>
                  : <div className="chat-error">{call.preview.reason ?? '研究对象不可读取'}</div>}
                {(call.preview.binding || call.preview.observation) && <div className="tool-call-reason">验证级别：{call.preview.verification_level}。限制：{call.preview.limitations?.join('、') ?? '未提供'}。</div>}
                {call.preview.observation && call.preview.run && <details><summary>来源 Run</summary><pre>{JSON.stringify(call.preview.run, null, 2)}</pre></details>}
                {call.preview.study && <details><summary>所属 Study</summary><pre>{JSON.stringify(call.preview.study, null, 2)}</pre></details>}
                {call.preview.proposal_sha256 && <div className="muted">提案摘要 SHA-256：{call.preview.proposal_sha256}</div>}
                {call.preview.design_sha256 && <div className="muted">设计摘要 SHA-256：{call.preview.design_sha256}</div>}
                {call.preview.design_sha256_matches === false && <div className="chat-error">模型提交的设计摘要与当前冻结快照不一致。</div>}
                {call.preview.approval_matches === false && <div className="chat-error">模型提交的审批摘要与当前 Study 不一致。</div>}
                {call.preview.budget_effect && <div className="tool-call-reason">预算影响：{call.preview.budget_effect}</div>}
                {call.preview.hypothesis && <details>
                  <summary>该设计关联的假设{call.preview.hypothesis_version_matches === false ? ' · 假设已有新版本' : ''}</summary>
                  <pre>{JSON.stringify(call.preview.hypothesis, null, 2)}</pre>
                </details>}
                {call.preview.execution_ready === false && <div className="tool-call-reason">执行就绪：否。草稿创建不会启动 Run。</div>}
                {call.preview.execution_readiness && <details>
                  <summary>执行路径审计 · {call.preview.execution_readiness.registered_design_route_ready ? '已注册候选入口前置条件通过，运行时仍须核验' : call.preview.execution_readiness.controller_routing_ready ? '原自动流程可路由，运行时仍须核验' : '当前入口前置条件未通过'}</summary>
                  <pre>{JSON.stringify(call.preview.execution_readiness, null, 2)}</pre>
                </details>}
              </details>}
              {call.risk === 'disclose' && <div className="tool-call-reason">{call.tool_name === 'research.status' ? '批准后，上述 Study 状态、预算、实体摘要及开发 Observation ID 将发送给已配置的模型服务；不包含确认集数值。' : call.tool_name === 'research.inspect_design' ? '批准后，以上设计、关联假设、状态版本和摘要将在后续推理中发送给已配置的模型服务。' : call.tool_name === 'research.inspect_claim' ? '批准后，以上 Claim、来源 Observation、数值、范围和限制将在后续推理中发送给已配置的模型服务；不会发送原始输入文件。' : call.tool_name === 'research.inspect_observation' ? '批准后，以上开发评估数值、来源 Run 和限制将在后续推理中发送给已配置的模型服务；不会发送原始输入或评分制品文件路径。' : '此工具会读取已登记研究状态；批准执行后，结果将在后续推理中发送给已配置的模型服务。'}</div>}
              {call.decision_reason && <div className="tool-call-reason">{call.decision_reason}</div>}
              {call.result != null && <details><summary>查看真实工具结果</summary><pre>{JSON.stringify(call.result, null, 2)}</pre></details>}
              {(call.error || call.validation_error) && <div className="chat-error" role="status">{call.error ? `${call.error.type} · ${call.error.message}` : `${call.validation_error?.code ?? call.validation_error?.type ?? '参数校验'} · ${call.validation_error?.message}`}</div>}
              {actionError && <div className="chat-error" role="alert">{actionError} · 操作尚未确认成功，请核对当前状态。</div>}
              {invalidApproval && ['pending_approval', 'approved'].includes(call.status) && <p className="muted">当前预览不可用或已过期，请重新获取服务端预览后审批。</p>}
              {call.status === 'pending_approval' && <div className="tool-call-actions">
                <button disabled={blocked || invalidApproval} onClick={() => { void act('approve') }}>{revisionApprovalLabel ?? (call.risk === 'disclose' ? '批准读取并披露' : call.tool_name === 'workspace.file_replace' ? '批准应用源码补丁' : call.tool_name === 'research.create_design' ? '批准登记设计草稿' : call.tool_name === 'research.create_hypothesis' ? '批准登记假设' : call.tool_name === 'research.freeze_design' ? '批准冻结设计' : call.tool_name === 'research.execute_design' ? '批准排队执行设计' : call.tool_name === 'research.study_action' ? '批准排队执行 Study' : call.tool_name === 'research.draft_create' ? '批准生成待审稿件' : '批准并执行')}</button>
                <button disabled={blocked} onClick={() => { void act('reject') }}>拒绝</button>
                <button disabled={blocked} onClick={() => { void act('cancel') }}>取消</button>
              </div>}
              {call.status === 'approved' && <div className="tool-call-actions">
                <button disabled={blocked || invalidApproval} onClick={() => { void act('execute') }}>继续执行已批准工具</button>
              </div>}
            </div>
  )
}
