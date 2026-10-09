# 统一研究运行时：离线工程验收（2026-10-09）

实施对象为原仓库 `C:\Users\wangj\Desktop\research\research-agent-v2`。
本轮完成了协议、运行时、入口和审计边界改造。最终 Windows / Linux 全量、沙箱、Docker、安装包及独立 LangGraph 补验均已通过，**离线工程验收完成**。
跳过项单列保留；本轮没有验证真实 Qwen 表现，也没有把历史付费结果或接口迁移验证泛化为通用科研能力证明。

本地代码提交为 `1353ead`、`8533779`、`514558e`、`ae82502`、`add3810`，未推送远端。
本轮真实 API 请求为 **0**；旧 FIGS 30 次额度账本及迁移试点 95 次真实发送账本保持原样。
原始轨迹、研究数据库、数据和安装制品留在本地，既有文档删除状态未改变。
机器可读结果见 `unified-runtime-validation-2026-10-09.json`。

## 本轮解决的结构性问题

- 动作说明、模型 JSON Schema 与程序解析共同来自 Pydantic 类型定义。执行、修订与停止分别定义；停止不再携带实验对象，模型解释另有主体、版本、范围和证据引用。
- 新项目通过 `experiment.json.metadata.planning_protocol = "research-runtime/v1"` 选择统一运行时。CLI、工作台、campaign 和迁移评测进入同一公共控制器边界。没有该元数据的历史项目保留旧行为；未知协议明确拒绝，不能静默退回旧通道。
- 研究状态、模型规划状态和科学判断分开记录。有效实验不会因后续模型回复错误消失；模型解释标为未经验证，不能填写权威分数、区间或覆盖独立判断。
- 请求、响应和动作完成收据进入原 ResearchStore 事务与事件链。JSON 报告只是投影，不是恢复权威。没有持久响应的歧义请求不能重发；完成的实验通过既有身份恢复。
- 网络重试、格式修正和语义修正共享同一逻辑请求的两次尝试与总截止时间。取消、发送阶段和用量预留沿用请求生命周期；未知用量不能因为重启或纠正释放为零。
- 主范围、诊断范围、指标方向和分析单位来自冻结项目声明。缺失、重复或矛盾映射在预检拒绝，不依据任务名或 s1/s2 名称猜测。历史声明采用可审计的 `legacy_derived` 投影。
- 新项目冻结开发指标选择规则后，由独立证据确定候选及 `registered-dev-selection/v1` 收据。模型停止和最终解释不能覆盖候选选择；开发判断为 inconclusive 时也必须保留其状态，确认结果仍由独立评估决定。
- 新审计器从权威记录重算范围、假设版本、Observation ID、指标、模型解释绑定和完成状态。已完成确认的部分研究仍保留规划失败及失败分母，不能通过修改报告宣布成功。

## 已取得的验证证据

- 协议、适配器及通用比较验证：20 passed、16 subtests passed。包含真实 MSE 控制器、错误范围映射预检、模型字段与独立事实分离、系数及制品篡改拦截。
- 非 FIGS 项目验证：冻结六个通用源码文件后，仅新增项目声明与多项式 CLI，采用 MSE、训练 seed 23、s9 主范围及 s7 诊断范围，完成公共 runtime 开发与独立确认。二次确认不新增 Run，接入前后通用源码哈希一致。
- 分析单位门禁：真实 MAE 夹具 40 行开发观察归为 20 个独立单位，即使点估计超过预测阈值，判断仍为证据不足。训练种子没有被计作独立样本。
- 恢复与权威审计：`unified-comparison-recovery.log` 为 15 passed。覆盖已写 Observation、尚未写主范围 Judgment 时硬中断，恢复补齐独立区间，不重跑实验、不重复结算；其中权威范围、身份、指标、解释与失败分母防篡改测试为 10 项。
- 检查顺序回归修复的相关验证：33 passed、1 skipped、11 subtests passed；跳过项不是通过。
- 先前源码轮次的 Linux bubblewrap 端到端：已通过，覆盖真实隔离后端及 init、search、freeze、confirm、status、report、replay 的本地链路。在 WSL 原生文件系统、非 root `research-runner` 用户下运行；最新版本的独立回执列于后文。
- 先前源码轮次的 Docker 构建、安装和非 root 入口：已通过。烟测无网络，无凭据或原确认数据库挂载。该结果不替代 Linux 沙箱、Linux 全量回归或最新源码验证。
- 先前 wheel：构建与隔离目录安装、协议／传输检查及 HTTP 工作台首页、19 个资源和会话检查通过。旧制品 SHA-256 为 `de0f2b0301045735c51530ddfc845a7a16423fe4fde765fa0cb9441414cb252c`，仅留作该轮证据，不能称作最新修复的最终安装包。

以上是离线工程与接口验证，不是模型科研表现评测。历史 27 单元付费试点仍保留原来的 19 个确认完成、8 个失败结果；本轮未补跑、未重写，也未用它替代新代码验收。

## 全量回归与保留失败

- 首次 Windows 全量：1629 passed、44 skipped、1 failed、759 subtests passed。失败为 `tests/test_harness.py::KernelKeepsProposalAuthorityTests::test_autonomous_code_without_harness_is_rejected` 的检查顺序回归，已修复并完成相关回归。原日志 `32758.15s` 包含会话中断及恢复等待，仅原样留痕，不用于性能分析。
- 首次 Linux 全量：1592 passed、81 skipped、1 failed、724 subtests passed，记录耗时 956.41s。同一 missing-harness 检查顺序回归已修复；该首次全量仍按失败保留。
- `514558e` 轮 Windows：1641 passed、44 skipped、1 failed、758 subtests passed，1169.38s。
- `514558e` 轮 Linux：1602 passed、81 skipped、3 failed、723 subtests passed，715.44s。失败包括早期 SSE 结束条件、缺少沙箱后端时的错误检查优先级，以及审计测试中 Windows 专属绝对路径夹具。检查优先级和跨平台夹具已修复，原记录保留。
- `ae82502` 轮 Windows：1641 passed、44 skipped、0 failed、759 subtests passed，1161.89s。该轮通过不替代后续源码的 Windows 验证。
- `ae82502` 轮 Linux：1603 passed、81 skipped、1 failed、724 subtests passed，748.80s。剩余 SSE 失败进一步定位为墙钟排序错误，详见下一节。
- 最新源码 `add3810` 的最终 Windows 全量：1646 passed、44 skipped、0 failed/error、759 subtests passed，1275.82s；JUnit 包含 2449 条记录。174 文件实现快照匹配，`source_unchanged=true`。回执 SHA-256 为 `5a9e71304823fa00177bc4e187a15b37af4add36c1ac188817f9abefba2623da`。
- 最新源码 `add3810` 的最终 Linux 全量：1610 passed、80 skipped、0 failed/error、724 subtests passed，774.00s。非 root UID 1000、WSL 原生 ext4；174 文件实现快照匹配，`source_unchanged=true`。Linux 回执 SHA-256 为 `162b914cb376d2e1485e5471b3e4ec5fe7f88bc8464120b208a1ee3293df2290`。
- Linux 跳过数由此前 81 变为 80，原因是 `test_doc_consistency::test_blind_report_is_internally_consistent` 所需的历史 aggregate 现已存在，跳过差异已经核对。该变化不是本轮新增功能指标。
- 最新源码 `add3810` 的 Linux bubblewrap 端到端：已通过。Windows 门禁目录为 `.acceptance/unified-runtime-causal-gates`，Linux 原生门禁目录为 `/home/research-runner/unified-runtime-causal-gates`。
- 最新源码 `add3810` 的 Docker：已通过，并与沙箱回执绑定同一份 174 文件实现快照。
- 最新 wheel 构建、安装与入口：已通过。SHA-256 为 `c0a18b28a7d03dc3493cd697c8872f31628977e3664ed3f797f86ebcda460085`，回执为 `.acceptance/causal-wheel-smoke/validation.json`。独立安装目录包含 Pydantic 2.14.0／pydantic-core 2.50.0；`python -I` 检查证明 popper 与 Pydantic 来自安装目录，168 个已安装 popper 源文件匹配冻结快照。
- 安装包烟测覆盖停止动作拒绝非法字段、同一逻辑操作两次尝试封顶、CLI help，以及 HTTP 首页、19 个资源内容和 session。安装包包含 19 个资源及 10 个 demo 文件；验证前后核心快照相同。安装包验证与已完成的 Windows / Linux 全量回归分别留有回执。
- LangGraph 独立 overlay 补验：Windows 与 Linux 各 90 passed、4 subtests passed、0 failed/skipped，分别为 85.52s、50.85s。两边原先跳过的 36 个 node ID 均通过；原主全量跳过记录保留，不把这 90 项直接加到主全量或当作 90 项全新测试。
- 补验未修改基础环境 pip 清单与生产源码；168 个 popper 源文件跨系统哈希一致。验证依赖为 LangGraph 1.2.14、checkpoint 4.2.0、checkpoint-sqlite 3.1.1、langchain-core 1.6.9、Pydantic 2.14.0。
- 真实新协议 LangGraph dev → freeze → confirm 在 Windows trusted-local 与 Linux sandboxed 下均完成：各 5 个 Run、11 个 Observation，checkpoint 与研究存储完整；重启 replay 新增 Run／Observation 均为 0，确认消费为 1，真实 API 请求为 0。辅助 target 冲突及烟测字段断言修正留在 tooling 记录中，没有记作生产失败。
- 平台与依赖跳过项按各自完整清单保留，不能把首次跳过数直接填入最终结果。LangGraph 跳过路径已有独立补验；其他未运行的平台或依赖检查仍保留未验证边界。

最终全量的跳过原因分组如下；这些条目没有计作主全量通过：

- Windows 44 项：LangGraph 可选依赖 36；ConPTY／pywinpty 3；未命中 TTC 字体夹具 1；符号链接权限 1；可用沙箱后端下不适用的 OSError 分支 2；Linux bubblewrap 专属检查 1。
- Linux 80 项：LangGraph 可选依赖 36；Windows 专属传输、执行、Worker、终端及权限检查 27；未复制真实第三方 vendor 源码及对应 worker 15；可用沙箱后端下不适用的 OSError 分支 2。
- 两边的 36 个 LangGraph 路径在独立 overlay 中已全部通过，单列补验，不回写主环境 skip，也不把相关 90 项重复加总。其余平台、数据或可选依赖检查仍按实际验证边界保留。

源码冻结记录为 `.acceptance/unified-runtime-causal-gates/source-freeze.json`：提交 `add3810f8a7e005ff69a6c943f1836be6c2dbef1`，174 文件实现指纹 `8f315041ac3b597728d62f91899c2f12bcd77a9977910467e9ea0fc47eb0f110`。冻结时的 668 文件归档与当前内容比较为 `diff=[]`；沙箱与 Docker 回执均确认源码匹配。两份旧预算账本经只读复核，SHA-256 与此前值完全一致。

最终完整性回执 `.acceptance/unified-runtime-causal-gates/final-integrity.json` 为 passed：四项平台门禁绑定同一 174 文件实现快照，wheel 前后快照与冻结核心相同，668 文件归档差异为空，两历史账本哈希未变，overlay 与 `add3810` 源码一致、168 个包源跨系统相同，真实 API 请求为 0。各失败轮次原样保留。

## SSE 失败的根因与修复证据

Linux 剩余失败由系统墙钟回拨 **98ms** 触发。旧代码按 `created` 时间排序父、子 turn；回拨后父 turn 在排序中落到子 turn 之后，续接逻辑因此误判为旧工具调用，导致 SSE 测试在应有最终响应之前结束。

`add3810` 改为使用持久事件 `seq` 表达因果顺序，不再用墙钟时间决定谁先发生。缺少事件记录的历史 turn 以 `rowid` 做只读兼容排序，已有时间字段及原事件均未修改。

- 确定性回拨夹具：旧实现稳定失败，新实现通过，保留 red → green 依据。
- Windows 前缀回归：93 passed。
- WSL 原生 Linux 前缀回归：93 passed。
- 最新 Windows / Linux 全量均已完成；上述针对性通过与安装包烟测仍作为各自独立证据保留。

## 支持范围与尚未验证

- `paired-unit-ci/v2` 当前支持 MAE/MSE 最小化、相对改善、独立单位等权聚合、10,000 次配对 bootstrap、95% 区间及至少 30 单位。制品验证器首版为 `polynomial/v1`；不能据此宣称支持任意模型、指标或科研领域。
- 项目适配器读取公开的范围与分析单位语义，但不自动证明领域假设、数据独立性或范围声明正确。这些仍需项目预注册和领域审查。
- 运行时修订能力仅追加解释文本版本。修改数值预测、候选空间或适用范围需要创建新的冻结项目，不能事后修改已评估版本。
- 无新付费模型评测，尚不能判断重构后 Qwen 的真实协议失败率、科学推理表现或相对策略优势。冻结实现后的未见项目验证是接口迁移证据，不是通用科研能力证明。
- 已定位的墙钟排序修复有确定性、前缀及最终全量回归依据；Windows / Linux 全量、沙箱、Docker、安装包和独立 overlay 的实现一致性已核对。平台专属、外部 vendor 数据及未安装可选依赖的剩余跳过项不计为已验证。

## 问题状态

以下“已修复”依据对应源码、针对性回归及最终离线工程门禁。

- **已修复：**动作 Schema、提示与解析使用统一类型；运行与恢复以原 ResearchStore 收据及事件链为权威，报告不再承担第二套状态权威。
- **已修复：**同一逻辑请求最多两次尝试，网络、格式与语义纠正共享截止时间，取消与发送阶段继续由请求生命周期控制。
- **已修复：**部分有效证据可恢复补齐独立判断；候选选择、确认身份与既有账本共同校验，确认幂等且不重复消费。
- **已修复：**墙钟回拨不再决定研究续接顺序；使用持久事件 seq，旧无事件记录只读兼容，不改历史事实。
- **已修复：**主范围、诊断范围与独立分析单位可声明，预检拒绝矛盾映射；审计核对主体、版本、范围、观察身份、指标、模型解释及失败分母。
- **外部限制：**无法从服务端或保留原始记录证实的历史 token 用量仍为 unknown。保守预留继续保留，不能反推数量、计为零或声称已经恢复。
- **仍未完成：**重构后真实 Qwen 的协议失败率、研究判断和策略表现验证；本轮没有发送付费请求。
- **外部限制／未验证：**44／80 个主全量跳过项按上文分组保留；其中 LangGraph 36 个路径有独立补验，其他平台、vendor 数据或可选依赖路径不冒充通过。

任意指标、任意模型序列化及任意科研领域均不在当前支持范围。本次交付状态是“完成离线工程验收”，不能泛化成全能科研能力验收。

本轮的目标是减少相互冲突的规则来源、固定科学事实与恢复权威，并让错误有界且可审计；不承诺新研究不会出错，也不把“所有任务显示成功”作为验收标准。
