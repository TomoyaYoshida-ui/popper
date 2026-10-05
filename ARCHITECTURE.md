# Popper Architecture

## 1. 架构目标

Popper 是一个本地优先、可扩展为团队与分布式部署的完整科研 Agent 平台。架构同时满足两类要求：

- 产品能力完整：工作区、Agent、文献、实验、算力、证据与写作形成端到端闭环；
- 科研内核可信：任何最终声明都能够追溯并重新验证。

系统采用“控制平面、执行平面、证据平面分离”的原则。界面、Agent 和 Worker 都不是科研事实的权威来源。

## 2. 分层架构

### 2.1 Experience Plane

面向研究者提供统一 Workbench：

- Project、Quest 与 Study 导航；
- 文献、研究问题和假设树；
- 实验图、运行状态和预算；
- 文件、代码编辑器、真实 PTY 和 Agent 对话；
- Observation、Decision、Claim 与审计事件；
- 报告、稿件和复现包。

前端只保存界面状态和用户意图。科研状态由 API 从 Research Kernel 投影，禁止浏览器直接构造最终 Observation 或修改历史证据。

### 2.2 Agent Runtime

Agent Runtime 统一接入模型与编码代理，职责包括：

- Harness 能力发现和模型配置；
- 会话、消息、turn、工具调用和 token 用量；
- 流式输出、中止、转向、排队与恢复；
- 计划审批、工具权限和人工确认；
- 子任务与长期 Campaign 的关联；
- 模型错误、限流和上下文压缩恢复。

Harness 不直接操作数据库。它产生结构化 Action，由应用服务验证身份、权限、前置状态和幂等键后交给 Research Kernel。

### 2.3 Research Kernel

Research Kernel 是系统最稳定的核心，拥有：

- Study 与研究范围；
- Hypothesis 及版本、机制、预测和证伪条件；
- ExperimentDesign 及冻结状态；
- Budget 与受限资源消费；
- Observation 及不确定性和工件引用；
- Decision 及备选解释和状态版本；
- Claim 及证据、适用范围和限制；
- Holdout/Confirmation 合同；
- 科研事件和状态快照。

所有状态转换必须在事务内验证不变量。Agent、UI、Worker 和插件只能调用公开 Action API。

### 2.4 Orchestration Plane

编排层负责长期研究流程，不拥有科学事实：

- Campaign DAG；
- 依赖、重试、退避和超时；
- 人工审批与可恢复中断；
- 补偿动作；
- Job 调度和并发限制；
- 外部文献、模型和执行服务降级。

每个步骤使用稳定幂等键。恢复时先读取 Research Kernel 和 Job Store 的事实，再决定继续、补偿或终止，不能依赖进程内存推断状态。

### 2.5 Execution Fabric

执行平面把不可变快照送到计算环境：

- Git commit/worktree 或内容寻址源码归档；
- 输入清单、环境清单、资源请求和入口命令；
- Local、Sandbox、Remote Worker，后续扩展 Docker、Kubernetes、Slurm；
- 日志流、心跳、取消、超时和进程树回收；
- 原始输出和工件上传；
- Worker 身份与执行信任等级。

统一的 `BackendDescriptor` 只描述如何提交、查询和取消运行。领域知识和最终指标不进入执行后端。

### 2.6 Evidence Plane

证据平面负责从原始运行事实产生可验证科研记录：

- 独立 Evaluator；
- 逐样本或原始测量结果；
- per-seed 统计和不确定性；
- 输入、源码、环境、评估器和工件摘要；
- append-only 事件链；
- 离线 replay；
- Claim–Evidence 图；
- 报告与稿件校验。

Evaluator 必须声明版本、代码摘要、输入合同和输出 schema。任何缺少这些字段的指标只能作为非受信诊断，不能形成最终 Observation。

### 2.7 Platform Infrastructure

个人本地版：

- SQLite；
- 本地 Git；
- 文件系统内容寻址工件库；
- 本地 Worker；
- 回环地址 API。

团队版目标：

- PostgreSQL 元数据与事务事件；
- S3 兼容对象存储；
- 持久化任务队列；
- Organization、RBAC 和项目成员；
- Secret Manager 与 Worker 凭据；
- 配额、成本和审计；
- OpenTelemetry 指标、日志和 trace；
- schema migration、备份和恢复。

本地版与团队版共享科研对象、Action API 和证据协议，区别只在基础设施适配器。

## 3. 核心实体

### 身份与工作区

- `Organization`
- `User`
- `Project`
- `Workspace`
- `Quest`
- `AgentSession`

### 科研对象

- `Study`
- `Hypothesis`
- `ExperimentDesign`
- `Observation`
- `Decision`
- `Claim`
- `BudgetLedger`
- `ConfirmationContract`

### 执行与证据

- `Revision`
- `ExperimentNode`
- `Run`
- `Job`
- `Artifact`
- `EvaluationReceipt`
- `EvidenceBinding`
- `AuditEvent`

每个实体使用全局唯一 ID、schema 版本、创建主体和时间。跨实体引用保存稳定 ID，不以目录名、界面索引或可修改标题作为身份。

## 4. 数据所有权

| 组件 | 拥有的数据 | 明确禁止 |
| --- | --- | --- |
| Research Kernel | 研究状态、预算、设计、观察、决策、声明 | 调用模型、执行任意代码 |
| Agent Runtime | 会话、消息、工具调用、用量、审批状态 | 直接写 Observation/Claim |
| Orchestrator | 步骤、依赖、重试、等待状态 | 把工作流完成等同科学结论成立 |
| Execution Fabric | Job、日志、资源、原始工件 | 自报最终指标 |
| Evidence Plane | 评分回执、摘要、replay、证据绑定 | 生成候选实现 |
| Workbench | 视图状态、用户操作意图 | 成为科研事实来源 |

## 5. 关键端到端流程

### 5.1 从问题到实验

1. 用户创建 Quest 和 Study。
2. 文献服务保存来源、全文状态和可定位证据。
3. Agent 提交 Hypothesis Action。
4. Research Kernel 验证范围、预算和版本，生成 Hypothesis。
5. Agent 或用户提交 ExperimentDesign。
6. 设计通过合同校验后冻结。
7. 实施服务从冻结设计创建不可变 Revision 与 ExperimentNode。

### 5.2 从运行到决策

1. Orchestrator 为节点创建 Run 和 Job。
2. Execution Fabric 提交不可变快照。
3. Worker 上传原始输出、日志和工件摘要。
4. Evaluator 独立产生 EvaluationReceipt。
5. Evidence Plane 验证工件和评估器，创建 Observation。
6. Agent 根据已有 Observation 提交 Decision Action。
7. Research Kernel 校验引用、状态版本和预算后提交 Decision。

### 5.3 最终确认与声明

1. 只有满足预注册条件的候选可以请求确认。
2. ConfirmationContract 原子消费受限资源。
3. 确认 Worker 看不到搜索阶段不应暴露的信息。
4. Evaluator 产生最终 Observation。
5. Claim 根据阈值、方向、不确定性和预定义规则生成。
6. 稿件中的数字通过 EvidenceBinding 引用 Claim/Observation。
7. 发布前执行 replay 和完整性检查。

当前落地的 EvidenceBinding 是 `research materialize evidence` 生成的确定性证据附录：
它从完整性通过的 Research Claim 与 Observation 派生 JSON/Markdown，校验时重新检查
冻结 manifest、输入、评估器、研究事件、来源制品和两份导出文件。它不把自由填写的
`materialize manuscript` 文本自动视为证据支持，也不构成便携复现或人工审校；
`materialize bound-draft` 只允许结构化的 Claim/作者说明块，要求该 Study 全部 Claim
各出现一次，结果段从绑定自动生成，作者说明标为未验证并拒绝裸数字。自由文本稿件
仍不是强制绑定稿件；正式投稿门禁仍需把每项事实声明映射到这些绑定并处理未绑定内容。
带绑定清单的草稿进入本地稿件事务流时必须走
`review-bound/submit-bound/publish-bound`：人工审校时冻结校验记录，提交须与审校
记录完全一致，发布须与提交记录一致；每次重新验证 Research Run、附录、稿件与
草稿结构。通用迁移入口拒绝跳过校验，
并对文件投影与实验事件链做一致性检查。这里的 published 只是本地记录，
`publication_attested=false`，并不证明外部发表。
稿件状态文件与事件链之间的崩溃窗口提供显式恢复：仅当经过 replay 的事件链与
JSON 迁移记录存在恰好一条合法的 JSON-only 尾部差异时，`paper recover-projection`
先提交带原投影摘要和被撤销迁移的恢复事件，再回滚 JSON 投影。恢复事件已写但文件
尚未修复时可幂等重试；其他分叉保持失败关闭，不自动猜测权威状态。

跨领域基准入口 `research benchmark/verify-benchmark` 复用内置非 ML DomainPack 与同一
Research Kernel，保留每个案例的项目、Research Run 和可用的证据附录；报告区分引擎
完整性验收与 Claim 结果，允许诚实的零 Claim 终态。其墙上时间仅描述该宿主的一次执行，
不作为模型能力、科学优越性或跨机器性能比较。
工作台通过持久化的 BenchmarkJobStore 异步运行该基准：当前工作区根拥有 Job、幂等键和
哈希事件链，后台线程只在成功领取 queued 状态后执行。重启时验证完整报告后才协调
running→succeeded；其余 running→interrupted，queued 可重新领取。报告读取不会把
失败案例或缺少 Claim 的终态伪装成成功发现。

## 6. 插件接口

稳定插件面包括：

- `Harness`：模型与编码 Agent；
- `LiteratureProvider`：检索和全文获取；
- `DomainProtocol`：领域输入、设计和测量合同；
- `Evaluator`：独立评分；
- `ExecutionBackend`：运行提交、状态、日志、取消；
- `ArtifactStore`：内容寻址存储；
- `Publisher`：报告、稿件和外部发布。

插件通过版本化 manifest 声明能力、权限、schema 和兼容范围。插件不能获得超出职责的数据库写权限。

## 7. 安全与信任模型

Popper 区分：

- 流程约束；
- OS 账户内隔离；
- 容器/命名空间隔离；
- 独立凭据域；
- 远程受信评估；
- 对抗性安全保证。

每个 Run、Artifact 和 EvaluationReceipt 都记录实际信任等级。未知代码默认进入隔离 Worker；可信本地模式必须由用户显式启用。密钥只通过受控 Secret Provider 注入，不写入消息、事件和工件。

## 8. API 与事件原则

- 外部 API 从 `/v1` 开始版本化；
- 写操作要求 `Idempotency-Key`；
- 错误采用稳定机器代码和人类说明；
- 所有写操作记录主体、项目、相关实体和 trace ID；
- 事件采用 append-only 语义，修正通过新事件表达；
- 大工件不进入数据库，通过内容摘要引用对象存储；
- schema 变更必须提供前向迁移和兼容读取策略。

事件哈希使用 `popper.eventlog` 的统一规范化协议，但领域 Store 自己解释事件和重放状态。
数据库事务通过 `TransactionConnection` 端口逐步解耦；SQLite 参考实现显式区分读事务与
`BEGIN IMMEDIATE` 写事务，并以保存点支持嵌套工作单元。该端口已经承载持久 revision
执行队列、worktree registry、Reproduction lifecycle、Agent Session/Job 与 Research Store/预算账本。PostgreSQL 尚未实现，必须通过 RFC 0002 的真实数据库等价性、并发和故障
验收后才能宣称支持，不能以接口存在代替运行证据。

## 9. 当前实现与目标架构

当前仓库已经具备 Research Kernel、Research Controller、沙箱 Worker、独立评分、证据重算、Campaign、文献/想法/代码提案、复现任务和工作台原型。

本地工作台对项目根及全部显式附加工作区根持有进程级独占锁；锁在启动恢复任何运行状态
之前取得，并在最后一个后台作业完成后释放。这样第二个工作台不会把仍在执行的 Agent
Turn、论文复现或 revision Job 误判为进程崩溃。该锁只保护同机本地工作台；跨主机团队
模式仍要求数据库租约与所有权机制。

当前 API v1 已实现 Workspace 与 Research 两条版本化命令通道。Research 命令在取得
SQLite 写锁后校验事件链与 `expected_version`，并把领域事件、快照、版本、幂等收据
及带 actor/trace 的审计事件原子提交。外部通道不持有 Run/Observation 写能力。

Agent Runtime 已建立本地持久化 Session/Turn 状态：多会话使用稳定 `ses_`/`trn_`
身份，v1 turn 在模型调用前落盘并由后台作业执行，成功、失败、用户取消和进程中断
均有明确终态；API v1 幂等重放不会重复调用模型，取消与迟到结果采用终态竞争保护。
每次 turn 状态转换还会在同一 SQLite 事务中追加带 actor/trace 的生命周期事件，支持
稳定顺序游标增量读取。结构化 ToolCall 使用稳定 `tcl_` 身份和持久化状态机；写工具由
服务端判定风险，必须经用户审批后执行，拒绝、取消、失败和重启中断均保留。当前只
接入 OpenAI/DeepSeek SSE chat harness、两个 Workspace Quest 工具、需要人工
授权披露的 `research.status` 只读研究摘要工具，以及经人工审批后委托 Research
Store 版本化命令执行的 `research.freeze_design`。后者只冻结预注册设计，不执行实验
或写 Observation/Claim；审批卡展示研究库权威设计预览。原生客户端支持
传输级协作取消：取消事件会主动关闭 HTTP 响应；文本 delta 持续写入 Session Store，
刷新或重连后可恢复草稿，终态写入后拒绝迟到 delta。更多 Harness 适配、用量统计和
流式工具执行仍属后续阶段。

Conversation Harness 使用独立版本化协议（当前 `1.0`）和能力描述符。能力集合明确
声明 text、streaming text、cooperative cancel、native tools 与 provider usage；运行时
只通过 `invoke` 协议调用，不再以任意对象属性猜测行为。Session 固定 harness 名称与
协议版本，重启时不兼容 Harness 会被拒绝；历史未版本化 `chat` Session 仅允许一次性
绑定到当前 v1 Harness。Harness 只生成文本或工具提议，仍无数据库和工具执行权。

模型执行不再直接等同于 Turn：每个异步 v1 Turn 会在同一事务中创建稳定 `job_`
Agent Job。Job Store 持久化 `queued/running/succeeded/failed/cancelled/interrupted`、
attempt、worker identity 与 lease；本地调度器限制并发槽。重启时仅续跑从未领取的
queued Job，崩溃时已 running 的 Job 和 Turn 显式中断，避免不受控重复模型调用；
Turn 已提交而 Job 尚未提交终态的窗口会从权威 Turn 状态自动协调。运行中的 worker
周期续约 lease，续约必须匹配领取者身份。OpenAI 兼容 provider 返回的 prompt、
completion 和 total token 用量进入 Job 账本并按 Session 聚合；缺失用量保持 0，
不以本地猜测替代供应商事实。

Session 可配置 nullable 的硬 token 额度。入队前，服务以完整 system/history、工具
schema、协议余量的 UTF-8 字节上界加最大输出 token 计算保守 reservation；事务内
校验“已消费 + 所有 queued/running 预留 + 新预留”后才创建 Turn/Job。Job 终结或
取消即释放预留，真实 provider usage 留在账本。额度不足返回稳定
`budget_exhausted`，且不会留下 Turn、Job 或模型调用。

货币成本不依赖内置“最新价格”。管理员通过 API 登记不可变价格目录，目录内容（名称、
ISO 三字母货币、逐模型输入/输出每百万 token 的微货币单价）生成内容寻址 `prc_` ID。
Session 在首个 Job 前选择目录；Job 快照 model 与 catalog ID，结束时只用 provider 的
真实 prompt/completion token 按该快照结算 `cost_micros`。已有 Job 后禁止换目录，避免
同一 Session 混合币种或用新价格重算历史。

绑定价格目录的 Session 还可设置微货币硬额度。queued/running Job 以保守 token 上界
乘输入/输出费率较高者预留费用；事务内校验“已结算费用 + 活跃费用预留 + 新预留”。
取消或终态释放预留，真实 usage 结算进 `cost_micros`；转向原子释放旧预留再申请新
预留。额度不足同样返回 `budget_exhausted`，不创建 Turn/Job 或调用 provider。

失败或中断后的重试是显式协议，不是把旧 Turn 改回 running：API 创建新的 Turn 与
queued Job，在 `parent_turn_id` 中保存谱系，并使用新的幂等键。旧错误、草稿、用量和
事件保持不可变；同一重试请求重放只返回已有子 Turn。重试链设有 10 层上限，防止
客户端故障形成无界自动循环。

运行中的 Turn 支持原子转向：服务在一个事务中把旧 Turn 标记为 `Steered`、取消旧
Job、保存已有草稿和用量，并创建带 `parent_turn_id` 的新 Turn 与 queued Job。提交后
才向旧传输发协作取消信号并调度新方向，因此进程崩溃不会留下“旧调用已停但新指令
未落盘”的缝隙；迟到的旧响应仍受终态栅栏约束。

OpenAI/DeepSeek 兼容聊天 Harness 使用原生 function-tool schema。模型只返回结构化
提议；助手消息与全部 ToolCall 在一个 Session Store 事务内提交，随后由工作台展示
审批卡。模型侧采用 API 合法的下划线函数名，服务端映射到内部稳定工具名，并重新
校验精确参数 schema、真实 Workspace ID 与状态版本。
后续 Turn 从持久化 ToolCall 读取已终结调用，以匹配 `tool_call_id` 的原生工具消息
反馈真实结果或失败／拒绝状态；未终结提议只作为待处理状态，不伪装成结果。历史裁剪
保留完整助手调用与工具回复组，避免悬空的工具引用。
同一 Turn 的工具全部终结后，Session Store 使用固定幂等键创建无虚构用户消息的
`agent_continuation` Turn 与持久化 Job，由工作台自动调度；单轮工具链上限为 8。
预算不足或调度中断时，`POST /api/v1/turns/{turn_id}/continue` 可幂等补发，
不得为已经存在后续用户 Turn 的旧工具提议创建新的继续推理。
Workspace 工具在工作区状态与命令幂等收据同一原子替换中提交。若 ToolCall 还停在
running 而进程重启，服务只读取并核对该收据，不重放命令；匹配则补记完成，缺失
或冲突则保留 OutcomeUnknown，不声称写操作没有发生。
Research 冻结工具使用 Research Store 同事务事件与命令收据；重启恢复还核对原命令
哈希与研究库完整性，通过后才补记 ToolCall 完成。
Agent 假设创建同样走 `ResearchStore.execute_command`：服务端验证精确字段、长度、
可检验预测、证伪条件和替代解释；内核在提交事务中生成 ID，版本门禁、`untested`
快照、事件、审计记录及命令收据一起落盘。Run、Observation、Decision 和 Claim
仍不对普通 Agent 工具开放直接写入。
Agent 设计草稿创建沿用同一事务命令路径，额外核对假设所属 Study、当前版本与
`untested` 状态。设计对象使用通用的干预、对照、指标、评分器、分析单位和重复取值
字段；创建只保证科研记录完整，不推定领域执行器已绑定，结果明确返回
`execution_ready=false`。现有 Controller 的配置映射与源码修订绑定仍须独立接线，
不得把草稿或冻结状态当作可执行性证明。
只读设计检查返回当前 Controller 路由前置条件的机器可读缺口，但不触发
`Experiment.verify_inputs()`、不物化 Revision、也不保留预算。它故意把
`controller_routing_ready` 与 `execution_ready` 分开：前者只是映射与协议一致性，
后者还需要输入身份、独立评分绑定和执行隔离在真正启动时通过。
Controller 现在另有 `execute_registered_design(design_id)` 窄入口：只接受同一
Study 下已冻结、假设版本一致的设计；干预必须精确匹配一个已有候选，且该配置
仍在底层 Experiment 的预注册集合内。对照、指标、评分器、重复协议与最小效应
也须和原研究一致。入口不改写 `research.json` 或候选集合，强制 sandbox，之后
复用既有预算、Run 登记、不可变代码快照与独立评估。Controller、CLI、工作台
按钮和需人工批准的 Agent 提议现已接入同一受限入口；任意新配置仍需建立新的
预注册实验。
对一个已完成开发运行的候选，不允许换个新设计再把同一核心结果登记成第二份
独立 Observation；仅同一设计已有 Run 时允许恢复其尚未完成的记账与评估。
完成后假设先标记为 `inconclusive`（已检验、尚无基线比较结论），不把单次开发
评分直接宣称为支持或证伪。
工作台设计执行队列 `DesignExecutionJobStore` 持久化冻结快照摘要、幂等排队、
原子领取和终态结果，并以哈希链校验事件及快照；重启后的 running Job 标记为
interrupted，不能无提示重放。`POST /api/v1/design-jobs` 需要本地可信会话、
明确的 Study 版本与设计摘要；后台 Job 重新核对冻结快照后才调用 Controller。
`GET /api/v1/design-jobs/{job_id}` 返回持久状态，同一设计与摘要的重试返回原 Job，
首次提交的过期 Study 版本则拒绝。工作台从设计卡审查冻结快照、显式确认后提交，
并轮询队列状态。Agent 可提出 `research.execute_design`，但只登记待批准的工具调用；
人审查冻结快照、版本、预注册匹配与预算影响后批准，服务端才排队。工具回执只包含
Job 身份与状态，不把排队解释成实验结论；重启恢复以持久 Job 身份核对工具结果。
若重启时 Job 仍标为 running，恢复逻辑先核对 Research Store 事件、实验溯源图、
冻结设计摘要及该设计成功 Run 的开发集 Observation；证据齐全才补记 Job 成功。
缺少任一证据则标记 interrupted，不重跑也不猜测结果。
设计审查分别报告原自动 Controller 路由与已注册候选窄入口资格；后者还会提示
重复证据、假设已检验、开发搜索关闭、外部确认已冻结和协议不一致等原因。
只读资格不替代 Controller 在真正执行前的输入、评分器和沙箱核验。
全新候选配置走 `research followup`，绝不扩写旧 Study 的 `research.json` 或
`Experiment.state`。命令要求新的确认集、至少一个父研究未预注册候选，验证父研究
事件/清单/输入身份，拒绝与父 train/dev/test 的摘要或样本身份重叠；它在新项目中
写入不可变输入血缘并初始化新的 Experiment 与 Study。血缘列出父 Study/清单摘要、
新确认集摘要和候选摘要，并随新 Study 清单冻结；旧研究结果只作探索背景。

Research Run 现在可以在登记事务中同时固定 `revision_id` 与 revision manifest
SHA-256；两个字段必须成对出现，不能在运行结束后补写。只读 Research 投影会跨
SQLite 科研状态与内容寻址 revision 工件构建 Experiment Provenance Graph，连接
Hypothesis、ExperimentDesign、CodeRevision、Run、Observation 与 Decision。投影读取
时重新验证 revision 身份、完整文件集合及摘要，并核对 Run–Revision–Design 三方关系；
历史未绑定 Run、缺失父修订、工件篡改和跨设计绑定均作为显式 integrity issue 返回，
不会在读取时自动修复。Workbench 已展示图节点/边统计与断链原因。

新 CodeRevision 的内容身份还固定源码控制来源：Git 项目记录 40 位 HEAD commit、
仓库内项目相对路径、源文件集合是否偏离 HEAD，以及 porcelain 状态摘要；非 Git 项目
明确记录为 `unversioned`。完整文件 SHA-256 仍是执行权威，Git 只提供可供人类审查的
祖先锚点。子 revision 继承父 revision 的来源锚点，不能用修复动作改写祖先来源。
外部确认 Run 不伪装成本地 revision，而是绑定已经验签的 confirmation submission、
control/candidate 角色、submission 摘要与代码文件清单摘要，并以 `external_source`
节点进入同一溯源图。

新 CodeRevision 在创建时生成规范化 unified diff；diff 摘要进入 revision identity，
正文随 manifest 一起传输和校验，因此远程 Worker、确认包与本地工作台看到的是同一份
冻结差异。`GET /api/v1/revisions/{revision_id}` 只在已登记 research run 中定位并验证
revision，返回 manifest 摘要、父版本、Git 来源、文件清单和冻结 diff。历史 revision
没有 diff 时返回 `null`，服务端禁止拿当前可变项目目录进行推测性重算。

带 Git 锚点的 revision 可以派生隔离的 detached worktree。控制器从固定 commit 创建
worktree，再把 revision 快照覆盖到仓库内项目路径并逐文件复核 SHA-256；这个副本可供
人工编辑，但永远不会反向写入 RevisionStore，也不能直接冒充新的可执行 revision。
worktree 生命周期使用独立 SQLite 注册表保存 `provisioning/active/failed/removed`、
幂等键、路径与错误。创建与移除 API 仅在 `--trusted-local` 下开放，校验同源请求，
删除前再次验证登记路径只能是专用临时根的直接子目录。进程重启后仍能列出并清理。
可信本地工作台在恢复执行 Job 前先对遗留 `provisioning` 作事务化对账：只有目录、
detached Git 锚点、项目路径和冻结文件 SHA-256 均验证通过才追加 `activated`；否则
追加 `failed`，保留可能不完整的目录供人工检查，不在启动时静默删除。
失败副本的清理是单独的用户动作：目录不存在且 Git 无残留登记时仅终结登记；目录存在时先核对它仍是
登记的 Git worktree 和固定 commit，再调用 Git 移除。校验不符时不递归删除替代内容。
移除的 Git/SQLite 跨系统窗口允许幂等重试：Git 目录已消失但事件尚未提交时，只有
`git worktree list` 也不再登记该路径才可补记 `removed`；遗留 Git 登记须先修复，
不能仅凭目录不存在宣称清理成功。

worktree 的编辑结果不能直接执行。`proposal` 操作只接受父 revision 已登记的 Python
文件修改或新增 Python 文件，拒绝删除、rename/copy、非代码文件和项目目录之外的修改；
每个 replacement 再经过路径、大小和 Python 语法验证。提案正文冻结在生命周期库中，
对外只暴露摘要。用户审批必须提交相同摘要，服务端使用冻结正文创建带
`parent_revision_id` 的内容寻址后继 revision；审批后的晚到 worktree 修改不会进入
新 revision。创建后继版本和重复审批均具备确定性、可恢复的幂等结果。

worktree 最新状态不是唯一审计来源。每次 provisioning、activated、failed、
proposal_frozen、submitted 和 removed 都在同一 SQLite 事务中追加事件，事件包含前序
摘要并形成全局 SHA-256 链。读取 API 会重算链、重放每个 worktree 的合法状态转换并
核对快照；任何事件载荷或当前状态被外部改写都会返回完整性失败。验证还会比对
provisioning 绑定的 revision/commit/幂等键、派生路径、提案正文摘要、后继 revision
与执行结果，避免只重放状态而漏掉静默改写的身份字段。工作台读取注册表时同步显示
完整性结果，单 worktree 的事件历史可通过版本化 API 审计。升级前仅有
快照的本地注册表会在首次打开时追加带 `migrated` 标记的合成生命周期，保留原快照并
明确区分迁移时间与原始动作时间，而不是把缺少历史误报为篡改。旧记录合成事件中的
路径没有历史证明，迁移标记保留这一边界。
所有 worktree 写入口在外部副作用前先验证整份注册表，损坏时 fail-closed；读接口仍
返回完整性诊断，不因写门禁而隐藏审计故障。

已审批后继 revision 通过 ResearchController 的显式窄入口执行。入口把冻结 child
重建为相对 parent 的精确 CodeEdit，并要求再次生成完全相同的 revision ID；随后复用
原有预算预留、Research Run 登记、沙箱 Worker、执行覆盖门禁、独立 Evaluator 和
Observation 写入。worktree 注册表记录 `execution_started` 以及 `executed` 或
`execution_failed`，成功时绑定权威 Run ID。崩溃恢复只接受同一 revision 已存在的
Run/Observation；同一 design 被其他 revision 观察时拒绝冒认。

后继执行由独立持久队列承接。`POST .../execute` 取得或创建稳定 `JOBR-*` 并立即
返回 202；后台领取后仍只调用 `ResearchController.execute_revision`。Job Store 保存
queued/running/终态、阶段、进度、取消标记、Run ID 与错误，并为生命周期建立独立
SHA-256 前向链。取消信号继续下传到已有 Worker，而不是只隐藏界面任务。重启时未领取
queued Job 重新调度，running Job 与 worktree 显式记为 interrupted/failed；科研副作用
可能已经发生，因此系统不做不受控的自动重放。

历史 Diff 迁移采用旁路证明而非身份回填。只有同时存在不可变 parent 与 child 快照时，
迁移器才按现行规范重建 unified diff，并写入 `DFA-*` attestation，绑定两份 manifest
SHA-256、Diff SHA-256、方法和创建时间。读取 API 每次重新计算快照差异并验证证明；
不会修改旧 `revision.json`，也不会把迁移结果宣称为原始身份冻结。无父快照的 root
revision 保持 unavailable，绝不读取当前项目目录推测过去状态。

领域重复语义由 DomainPack 而非 ML 默认值决定。Research manifest 同时冻结
`repeat_unit` 与开发划分的 `repeat_values`；Worker 和独立 Evaluator 在每个 split 上
重新按域包导出实际单位。`analysis_unit` 因而使用区块/群组 ID，`independent_run`
使用独立重复编号，只有 `train_seed` 才表示训练随机种子。兼容字段 `seeds` 暂时保留，
但不再拥有领域语义。ODE、公开诊断数据与处理效应研究均通过同一 ResearchController、
预算、Revision、Run、Observation、Decision 和 Provenance Graph 验收。

论文复现状态机通过兼容投影进入同一溯源词汇：paper external source、冻结
protocol、runner/verifier code snapshot、Run、Artifact、Observation 与 Decision。
领域 verifier 只校验输出内部一致性；核心另行按照冻结 metric、reported value 与
absolute tolerance 计算复现裁定，避免 `status=verified` 被误当作论文主张成立。该层
生命周期以 `.popper-reproduction/reproduction.sqlite` 为写权威。每次执行领取、完成、
核验、失败和中断恢复都在 `BEGIN IMMEDIATE` 事务中更新快照并追加前向哈希事件，读取时
重放合法阶段迁移并核对链尾快照；并发领取只有一个获胜者。`state.json` 只是可丢失、
可重建的兼容投影，既不参与裁定，也不能覆盖 SQLite 状态。进程重启会将残留的
`running/verifying` 追加为明确的 interrupted 终态。

近期架构工作重点不是增加更多孤立能力，而是：

1. 统一核心 ID、实体关系和 schema；
2. 将所有主流程收敛到版本化应用 API；
3. 把 Agent 会话和 Campaign 纳入统一恢复语义；
4. 在已统一的规范化哈希链内核之上，继续抽取 SQLite/PostgreSQL 事务存储适配器；领域状态重放仍由各内核负责，不能降级成通用键值事件；
5. 分离本地存储适配器和团队基础设施适配器；
6. 让唯一 Workbench 完整覆盖真实端到端流程。
