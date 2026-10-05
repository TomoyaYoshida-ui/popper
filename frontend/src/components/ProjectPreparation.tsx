import { useEffect, useRef, useState } from 'react'
import { commitProjectPreparation, initializeStudy, previewProjectPreparation, previewStudyInitialization,
  type ProjectPreparationPreview, type StudyInitializationPreview } from '../api'
import { useConfirmation } from './ConfirmProvider'

type Mode = 'import' | 'create'
type Stage = 'project' | 'protocol' | 'review' | 'study'
type Preparation = { key: string; path: string; protocol: string; files: string; mode: Mode; stage: Stage;
  registeredWorkspaceId?: string; registeredPath?: string; fileRows?: FileRow[];
  studyBudget?: string; experimentBudget?: number }
type FileRow = { id: string; path: string; content: string }
const stages: Array<{ key: Stage; label: string }> = [
  { key: 'project', label: '项目' }, { key: 'protocol', label: '协议与文件' },
  { key: 'review', label: '校验与预注册' }, { key: 'study', label: '建立 Study' },
]
function readPreparation(key: string): Preparation {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? '{}') as Partial<Preparation>
    return { key, path: typeof value.path === 'string' ? value.path : '',
      protocol: typeof value.protocol === 'string' ? value.protocol : '', files: typeof value.files === 'string' ? value.files : '{}',
      mode: value.mode === 'create' || (!value.mode && !!value.protocol) ? 'create' : 'import',
      stage: stages.some((stage) => stage.key === value.stage) ? value.stage! : 'project',
      registeredWorkspaceId: value.registeredWorkspaceId, registeredPath: value.registeredPath,
      studyBudget: typeof value.studyBudget === 'string' ? value.studyBudget : undefined,
      experimentBudget: typeof value.experimentBudget === 'number' && Number.isFinite(value.experimentBudget) ? value.experimentBudget : undefined,
      fileRows: Array.isArray(value.fileRows) ? value.fileRows.filter((row) => typeof row?.id === 'string' && typeof row?.path === 'string' && typeof row?.content === 'string') : undefined }
  } catch { return { key, path: '', protocol: '', files: '{}', mode: 'import', stage: 'project' } }
}
function object(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
function initialProtocol(): Record<string, unknown> {
  return { name: '', objective: '', entrypoint: 'model.py', code_files: ['model.py'],
    train: 'train.json', dev: 'dev.json', test: 'test.json', metric: { name: '', direction: 'max' },
    baseline: {}, candidates: [{}], budget: 4, timeout_seconds: 60, min_improvement: 0, seeds: [] }
}
function scalarValue(text: string): unknown {
  try { return JSON.parse(text) as unknown } catch { return text }
}
function ConfigFields({ title, value, onChange }: { title: string; value: Record<string, unknown>; onChange: (next: Record<string, unknown>) => void }) {
  const entries = Object.entries(value)
  return <div className="config-fields"><strong>{title}</strong>{entries.map(([key, item]) => <div className="config-row" key={key}>
    <input aria-label={`${title}参数名 ${key}`} defaultValue={key} onBlur={(event) => {
      const nextKey = event.target.value.trim()
      if (!nextKey || nextKey === key || nextKey in value) { event.target.value = key; return }
      const next = { ...value }; delete next[key]; next[nextKey] = item; onChange(next)
    }} />
    <input aria-label={`${title}参数值 ${key}`} defaultValue={typeof item === 'string' ? item : JSON.stringify(item)}
      onBlur={(event) => onChange({ ...value, [key]: scalarValue(event.target.value) })} />
    <button type="button" className="mini" aria-label={`移除${title}参数 ${key}`} onClick={() => { const next = { ...value }; delete next[key]; onChange(next) }}>移除</button>
  </div>)}<button type="button" className="mini" onClick={() => {
    let index = entries.length + 1; while (`param_${index}` in value) index++
    onChange({ ...value, [`param_${index}`]: '' })
  }}>＋ 添加参数</button></div>
}

export function ProjectPreparation({ workspaceKey, trustedLocal, currentProjectName, onProjectChanged, onResearchChanged,
  onRequestProjectChange, onStudyCreated }: { workspaceKey: string | null; trustedLocal: boolean; currentProjectName?: string;
  onProjectChanged?: () => Promise<void>; onResearchChanged: () => void;
  onRequestProjectChange?: (action: () => void) => void; onStudyCreated: (runDir: string, studyId?: string) => void }) {
  const confirmation = useConfirmation()
  const prepKey = `popper-project-prep:${workspaceKey ?? 'default'}`
  const [preparation, setPreparation] = useState(() => readPreparation(prepKey))
  const prep = preparation.key === prepKey ? preparation : readPreparation(prepKey)
  const [protocolEditor, setProtocolEditor] = useState<'guided' | 'json'>('guided')
  const [fileRows, setFileRows] = useState<FileRow[]>([])
  const [filesError, setFilesError] = useState<string | null>(null)
  const [projectPreview, setProjectPreview] = useState<ProjectPreparationPreview | null>(null)
  const [projectBusy, setProjectBusy] = useState(false)
  const [projectError, setProjectError] = useState<string | null>(null)
  const [storageError, setStorageError] = useState<string | null>(null)
  const [studyRunName, setStudyRunName] = useState('study-1')
  const [studyBudget, setStudyBudget] = useState(() => prep.studyBudget ?? '4')
  const [studyPreview, setStudyPreview] = useState<StudyInitializationPreview | null>(null)
  const [studyBusy, setStudyBusy] = useState(false)
  const [studyError, setStudyError] = useState<string | null>(null)
  const requestInFlight = useRef(false)
  const context = useRef(workspaceKey)
  context.current = workspaceKey
  const registered = !!workspaceKey && prep.registeredWorkspaceId === workspaceKey && prep.registeredPath === prep.path
  const activeStage = prep.stage === 'study' && !registered ? 'project' : prep.stage
  let protocol: Record<string, unknown> | null = null
  try { protocol = object(prep.protocol.trim() ? JSON.parse(prep.protocol) : initialProtocol()) } catch { /* Advanced repair remains available. */ }
  const metric = object(protocol?.metric) ?? {}
  const candidates = Array.isArray(protocol?.candidates) ? protocol.candidates : []
  const experimentBudget = prep.experimentBudget ?? (prep.protocol.trim() && typeof protocol?.budget === 'number' ? protocol.budget : null)
  const update = (value: Partial<Preparation>, invalidate = true) => {
    const affectsInput = invalidate && ['path', 'protocol', 'files', 'fileRows', 'mode'].some((key) => key in value)
    setPreparation((current) => ({ ...(current.key === prepKey ? current : readPreparation(prepKey)), ...value,
      ...(affectsInput ? { registeredWorkspaceId: undefined, registeredPath: undefined } : {}) }))
    if (invalidate) { setProjectPreview(null); setStudyPreview(null); setProjectError(null) }
  }
  const setProtocol = (key: string, value: unknown) => { if (protocol) update({ protocol: JSON.stringify({ ...protocol, [key]: value }, null, 2) }) }
  const field = (key: string) => typeof protocol?.[key] === 'string' ? String(protocol[key]) : ''
  useEffect(() => {
    const restored = readPreparation(prepKey)
    setPreparation(restored); setStudyBudget(restored.studyBudget ?? '4'); setProjectPreview(null); setProjectError(null)
    setStudyPreview(null); setStudyError(null)
  }, [prepKey])
  useEffect(() => {
    if (preparation.key !== prepKey) return
    try { localStorage.setItem(prepKey, JSON.stringify(preparation)); setStorageError(null) }
    catch { setStorageError('本地项目草稿无法保存。当前输入仍保留，请勿在完成前关闭页面。') }
  }, [prepKey, preparation])
  useEffect(() => {
    try {
      const value = object(JSON.parse(prep.files))
      if (!value || Object.values(value).some((entry) => typeof entry !== 'string')) throw new Error('初始文件必须是路径到文本的 JSON 对象')
      const rows = prep.fileRows ?? Object.entries(value).map(([path, content], index) => ({ id: `restored-${index}`, path, content: String(content) }))
      setFileRows(rows)
      setFilesError(rows.some((row) => !row.path.trim()) || new Set(rows.map((row) => row.path.trim())).size !== rows.length ? '每个文件需要唯一的相对路径。' : null)
    } catch (cause) { setFilesError((cause as Error).message) }
  }, [prepKey])
  const updateFiles = (rows: FileRow[]) => {
    setFileRows(rows)
    const names = rows.map((row) => row.path.trim())
    if (names.some((name) => !name) || new Set(names).size !== names.length) {
      setFilesError('每个文件需要唯一的相对路径。'); update({ fileRows: rows }); return
    }
    setFilesError(null)
    update({ fileRows: rows, files: JSON.stringify(Object.fromEntries(rows.map((row) => [row.path.trim(), row.content])), null, 2) })
  }
  const inputs = () => {
    if (prep.mode === 'import') return { protocol: null, files: {} }
    if (filesError) throw new Error(filesError)
    const value = object(JSON.parse(prep.protocol.trim() || JSON.stringify(initialProtocol())))
    const files = object(JSON.parse(prep.files))
    if (!value) throw new Error('协议必须是 JSON 对象')
    if (!files || Object.values(files).some((item) => typeof item !== 'string')) throw new Error('初始文件必须是路径到文本的 JSON 对象')
    return { protocol: value, files: files as Record<string, string> }
  }
  const review = async () => {
    if (requestInFlight.current) return
    requestInFlight.current = true; setProjectBusy(true); setProjectError(null)
    const captured = workspaceKey
    try {
      const value = inputs(); const result = await previewProjectPreparation(prep.path.trim(), value.protocol, value.files)
      if (context.current === captured) { setProjectPreview(result); update({ stage: 'review' }, false) }
    } catch (cause) { if (context.current === captured) setProjectError((cause as Error).message) }
    finally { requestInFlight.current = false; setProjectBusy(false) }
  }
  const register = () => {
    if (!projectPreview || requestInFlight.current) return
    const commit = async () => {
      if (requestInFlight.current) return
      requestInFlight.current = true; setProjectBusy(true); setProjectError(null)
      try {
        const value = inputs()
        const result = await commitProjectPreparation(projectPreview, value.protocol, value.files)
        const next: Preparation = { ...prep, key: `popper-project-prep:${result.workspace_id}`, path: result.project_path,
          stage: 'study', registeredWorkspaceId: result.workspace_id, registeredPath: result.project_path,
          studyBudget: String(Math.max(4, projectPreview.experiment_budget + 2)), experimentBudget: projectPreview.experiment_budget }
        try { localStorage.setItem(next.key, JSON.stringify(next)) } catch { setStorageError('项目已预注册，但本地引导状态未能保存；请重新校验以恢复。') }
        if (context.current === result.workspace_id) { setPreparation(next); setStudyBudget(next.studyBudget!) }
        if (onProjectChanged) await onProjectChanged(); else onResearchChanged()
      } catch (cause) { setProjectError((cause as Error).message) }
      finally { requestInFlight.current = false; setProjectBusy(false) }
    }
    if (onRequestProjectChange) onRequestProjectChange(() => { void commit() }); else void commit()
  }
  const previewStudy = async () => {
    if (!registered || requestInFlight.current) return
    requestInFlight.current = true; setStudyBusy(true); setStudyError(null)
    const captured = workspaceKey
    try {
      const result = await previewStudyInitialization(studyRunName.trim(), Number(studyBudget))
      if (context.current === captured) setStudyPreview(result)
    } catch (cause) { if (context.current === captured) setStudyError((cause as Error).message) }
    finally { requestInFlight.current = false; setStudyBusy(false) }
  }
  const createStudy = async () => {
    if (!registered || !studyPreview || requestInFlight.current) return
    if (!await confirmation({ title: '登记新的 Study', description: '冻结本次研究契约并登记 Study。此操作不会执行实验。',
      details: [{ label: '项目', value: studyPreview.project }, { label: '预算上限', value: String(studyPreview.budget_cap) }, { label: '研究目录', value: studyPreview.run_dir }], confirmLabel: '确认并登记 Study' })) return
    requestInFlight.current = true; setStudyBusy(true); setStudyError(null)
    try {
      const created = await initializeStudy(studyPreview.run_name, studyPreview.budget_cap, studyPreview.proposal_sha256)
      setStudyPreview(null); onStudyCreated(created.run_dir, created.study_id); onResearchChanged()
    } catch (cause) { setStudyError((cause as Error).message) }
    finally { requestInFlight.current = false; setStudyBusy(false) }
  }
  if (!trustedLocal) return <div className="notice">当前是只读工作台。可查看已有研究；项目预注册和 Study 创建需要受信任本地模式。</div>
  return <section className="project-preparation" aria-label="项目准备向导">
    <ol className="preparation-steps" aria-label="准备进度">{stages.map((stage, index) => <li key={stage.key}>
      <button type="button" aria-current={activeStage === stage.key ? 'step' : undefined} disabled={projectBusy || studyBusy || (stage.key === 'study' && !registered)}
        onClick={() => update({ stage: stage.key }, false)}>{index + 1}. {stage.label}</button></li>)}</ol>
    {storageError && <div className="notice err" role="alert">{storageError}</div>}
    {activeStage === 'project' && <div className="section box prep-card"><h2>准备科研项目</h2>
      <div className="editor-switch" role="group" aria-label="项目来源">
        <button type="button" disabled={projectBusy} className={prep.mode === 'import' ? 'active' : ''} onClick={() => update({ mode: 'import' })}>导入已有项目</button>
        <button type="button" disabled={projectBusy} className={prep.mode === 'create' ? 'active' : ''} onClick={() => update({ mode: 'create', protocol: prep.protocol || JSON.stringify(initialProtocol(), null, 2) })}>新建项目</button>
      </div>
      <p className="muted">{prep.mode === 'import' ? '读取目录中已有的 experiment.json 和登记文件；不会覆盖已有协议或文件。' : '填写协议和初始文本文件，校验通过后才写入目录。不会自动生成代码或调用模型。'}</p>
      <label>项目目录<input aria-label="项目目录" disabled={projectBusy} value={prep.path} placeholder="本机绝对路径" onChange={(event) => update({ path: event.target.value, registeredWorkspaceId: undefined, registeredPath: undefined })} /></label>
      <button className="btn primary" disabled={projectBusy || !prep.path.trim()} onClick={() => { if (prep.mode === 'import') void review(); else update({ stage: 'protocol' }, false) }}>{projectBusy ? '正在校验…' : prep.mode === 'import' ? '读取并校验项目' : '下一步：协议与文件'}</button>
    </div>}
    {activeStage === 'protocol' && <div className="section box prep-card"><h2>{prep.mode === 'import' ? '导入已有协议' : '协议与初始文件'}</h2>
      {prep.mode === 'import' ? <><p className="muted">将读取 {prep.path} 中已有协议和文件，无需重新输入。</p><button className="btn primary" disabled={projectBusy} onClick={() => void review()}>读取并校验项目</button></> : <>
      <div className="editor-switch" role="group" aria-label="协议编辑方式"><button type="button" className={protocolEditor === 'guided' ? 'active' : ''} onClick={() => setProtocolEditor('guided')}>结构化编辑</button><button type="button" className={protocolEditor === 'json' ? 'active' : ''} onClick={() => setProtocolEditor('json')}>高级 JSON</button></div>
      {protocolEditor === 'guided' && protocol && <fieldset className="protocol-grid" disabled={projectBusy}>
        <label className="wide">研究名称<input value={field('name')} onChange={(event) => setProtocol('name', event.target.value)} /></label>
        <label className="wide">研究目标<textarea rows={2} value={field('objective')} onChange={(event) => setProtocol('objective', event.target.value)} /></label>
        <label>执行入口<input value={field('entrypoint')} onChange={(event) => setProtocol('entrypoint', event.target.value)} /></label>
        <label>指标名称<input value={typeof metric.name === 'string' ? metric.name : ''} onChange={(event) => setProtocol('metric', { ...metric, name: event.target.value })} /></label>
        <label>优化方向<select value={metric.direction === 'min' ? 'min' : 'max'} onChange={(event) => setProtocol('metric', { ...metric, direction: event.target.value })}><option value="max">越高越好</option><option value="min">越低越好</option></select></label>
        {(['budget', 'min_improvement', 'timeout_seconds'] as const).map((key) => <label key={key}>{{ budget: '开发预算', min_improvement: '最小有意义改进', timeout_seconds: '运行超时（秒）' }[key]}<input type="number" step={key === 'min_improvement' ? 'any' : '1'} value={typeof protocol[key] === 'number' ? String(protocol[key]) : ''} onChange={(event) => setProtocol(key, event.target.value ? Number(event.target.value) : undefined)} /></label>)}
        {(['train', 'dev', 'test'] as const).map((key) => <label key={key}>{{ train: '训练数据', dev: '开发数据', test: '独立确认数据' }[key]}<input value={field(key)} onChange={(event) => setProtocol(key, event.target.value)} /></label>)}
        <label className="wide">代码文件（逗号分隔）<input value={Array.isArray(protocol.code_files) ? protocol.code_files.join(', ') : ''} onChange={(event) => setProtocol('code_files', event.target.value.split(',').map((item) => item.trim()).filter(Boolean))} /></label>
        <label className="wide">重复种子（可选，逗号分隔）<input defaultValue={Array.isArray(protocol.seeds) ? protocol.seeds.join(', ') : ''} onBlur={(event) => setProtocol('seeds', event.target.value.split(',').map((item) => item.trim()).filter(Boolean).map(Number))} /></label>
        <div className="wide"><ConfigFields title="基线配置" value={object(protocol.baseline) ?? {}} onChange={(next) => setProtocol('baseline', next)} /></div>
        <div className="wide config-candidates"><strong>候选配置</strong>{candidates.map((item, index) => <div className="candidate-card" key={index}><div className="candidate-head"><span>候选 {index + 1}</span><button type="button" className="mini" onClick={() => setProtocol('candidates', candidates.filter((_, i) => i !== index))}>移除候选</button></div>{object(item) ? <ConfigFields title={`候选 ${index + 1}`} value={object(item)!} onChange={(next) => setProtocol('candidates', candidates.map((candidate, i) => i === index ? next : candidate))} /> : <p className="notice err">请在高级 JSON 中修复此候选。</p>}</div>)}<button type="button" className="mini" onClick={() => setProtocol('candidates', [...candidates, {}])}>＋ 添加候选</button></div>
        <p className="muted wide">领域包由服务端根据指标校验。其他领域字段、嵌套参数与未知字段始终保留，可在高级 JSON 中核对。</p>
      </fieldset>}
      {protocolEditor === 'guided' && !protocol && <div className="notice err">协议 JSON 尚未形成对象，请切换到高级 JSON 修复。</div>}
      {protocolEditor === 'json' && <label>完整协议 JSON<textarea aria-label="完整协议 JSON" disabled={projectBusy} rows={12} value={prep.protocol} onChange={(event) => update({ protocol: event.target.value })} /></label>}
      <h3>初始文本文件</h3><p className="muted">填写协议登记的代码和数据文件。已有文件可不提供；保存前服务端会检查路径、内容和协议归属。</p>
      {fileRows.map((row, index) => <fieldset className="initial-file-card" key={row.id} disabled={projectBusy}><label>文件路径<input aria-label={`初始文件路径 ${index + 1}`} value={row.path} onChange={(event) => updateFiles(fileRows.map((item) => item.id === row.id ? { ...item, path: event.target.value } : item))} /></label><label>文件内容<textarea aria-label={`初始文件内容 ${index + 1}`} rows={6} value={row.content} onChange={(event) => updateFiles(fileRows.map((item) => item.id === row.id ? { ...item, content: event.target.value } : item))} /></label><button type="button" className="mini" onClick={() => updateFiles(fileRows.filter((item) => item.id !== row.id))}>移除此文件</button></fieldset>)}
      <button type="button" disabled={projectBusy} className="mini" onClick={() => updateFiles([...fileRows, { id: crypto.randomUUID(), path: `file-${fileRows.length + 1}.txt`, content: '' }])}>＋ 添加文本文件</button>
      <details className="prep-advanced"><summary>高级 · 文件映射 JSON</summary><label>初始文件 JSON<textarea aria-label="初始文件 JSON" disabled={projectBusy} rows={5} value={prep.files} onChange={(event) => {
        update({ files: event.target.value, fileRows: undefined }); try { const files = object(JSON.parse(event.target.value)); if (!files || Object.values(files).some((entry) => typeof entry !== 'string')) throw new Error('文件内容必须为文本'); setFileRows(Object.entries(files).map(([path, content], index) => ({ id: `json-${index}`, path, content: String(content) }))); setFilesError(null) } catch (cause) { setFilesError((cause as Error).message) }
      }} /></label></details>
      {filesError && <div className="notice err" role="alert">{filesError}</div>}
      <div className="actions"><button className="btn" onClick={() => update({ stage: 'project' }, false)}>返回项目</button><button className="btn primary" disabled={projectBusy || !!filesError || !prep.path.trim()} onClick={() => void review()}>{projectBusy ? '正在校验…' : '校验项目与协议'}</button></div>
      </>}
    </div>}
    {activeStage === 'review' && <div className="section box prep-card"><h2>核对预注册范围</h2>{!projectPreview ? <><p className="muted">校验预览不跨刷新恢复，请重新核对最新输入。</p><button className="btn" disabled={projectBusy} onClick={() => void review()}>重新校验</button></> : <>
      <dl className="resource-facts"><div><dt>目标项目</dt><dd>{projectPreview.name}<br />{projectPreview.project_path}</dd></div><div><dt>研究目标</dt><dd>{projectPreview.objective}</dd></div><div><dt>领域与指标</dt><dd>{projectPreview.domain_pack} · {projectPreview.metric.name}（{projectPreview.metric.direction === 'max' ? '越高越好' : '越低越好'}）</dd></div><div><dt>代码</dt><dd>{projectPreview.entrypoint} · {projectPreview.code_files.join('、')}</dd></div><div><dt>数据范围</dt><dd>训练 {projectPreview.data_files.train} · 开发 {projectPreview.data_files.dev} · 独立确认 {projectPreview.data_files.test}</dd></div><div><dt>重复与预算</dt><dd>{projectPreview.repeat_unit} × {projectPreview.repeat_values.length || '由数据导出'} · 候选 {projectPreview.candidate_count} · 开发预算 {projectPreview.experiment_budget}</dd></div></dl>
      <p className="notice">{projectPreview.already_initialized ? '已有预注册状态；确认后登记并切换到此项目。' : '确认后写入已校验文件、预注册并切换到此项目；不会执行实验。'}</p>
      <div className="actions"><button className="btn" disabled={projectBusy} onClick={() => update({ stage: prep.mode === 'create' ? 'protocol' : 'project' }, false)}>返回修改</button><button className="btn primary" disabled={projectBusy} onClick={register}>{projectBusy ? '正在预注册…' : '确认预注册并切换项目'}</button></div>
      <details><summary>输入与审批标识</summary><p>已校验输入 {Object.keys(projectPreview.input_hashes).length} 个</p><pre>{projectPreview.approval_sha256}</pre></details>
    </>}</div>}
    {activeStage === 'study' && registered && <div className="section box prep-card"><h2>为已预注册项目建立 Study</h2><p className="notice">当前目标：{currentProjectName ?? prep.registeredPath} · 已完成预注册并切换。Study 不会自动运行代码。</p>
      <div className="actions"><label>运行名称<input value={studyRunName} disabled={studyBusy} onChange={(event) => { setStudyRunName(event.target.value); setStudyPreview(null) }} /></label><label>Study 预算上限<input type="number" min="4" max="10000" value={studyBudget} disabled={studyBusy} onChange={(event) => { setStudyBudget(event.target.value); update({ studyBudget: event.target.value }, false); setStudyPreview(null) }} /></label><button className="btn primary" disabled={studyBusy || !studyRunName.trim()} onClick={() => void previewStudy()}>{studyBusy ? '正在核对…' : '预览 Study 契约'}</button></div>
      <p className="muted">{experimentBudget != null ? `开发预算 ${experimentBudget} + 独立确认预留 2，建议 Study 预算至少 ${Math.max(4, experimentBudget + 2)}。` : 'Study 预算需要覆盖开发评估，并额外为独立确认预留 2 次评分。'}可按研究计划调整；此建议不会修改已有 Study 的预算。</p>
      <p className="muted">仅适用于已初始化、输入未变且仍处于搜索阶段的项目。实际预算和登记条件由服务端核验。</p>
      {studyPreview && <div className="notice"><div>项目：{studyPreview.project}</div><div>目标：{studyPreview.objective}</div><div>指标 {studyPreview.metric.name} · 候选 {studyPreview.candidate_count} · 预算上限 {studyPreview.budget_cap}</div><div>研究目录：{studyPreview.run_dir}</div><button className="btn primary" disabled={studyBusy} onClick={() => void createStudy()}>确认并登记 Study</button></div>}
      {studyError && <div className="notice err" role="alert">{studyError}</div>}
    </div>}
    {projectError && <div className="notice err" role="alert">{projectError}</div>}
  </section>
}
