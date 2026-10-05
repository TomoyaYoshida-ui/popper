# Agent 执行层机制改造与验收记录

## 范围与证据

保留 Python Harness、会话存储、Research Kernel 和原科学协议。本轮不引入多个 Agent SDK，不修改数值候选或确认规则。参考实现：

- [OpenCode 工具调用处理](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm.ts)：无效调用反馈机制。
- [mini-swe-agent 执行循环](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/agents/default.py)：有界反馈循环。
- [Aider 编辑格式](https://aider.chat/docs/more/edit-formats.html)：结构化 SEARCH/REPLACE。Popper 采用更保守的唯一精确匹配，不应用模糊补丁。

已核实平台根因：第二闭环运行的完整 Revision 预览回执有审批摘要，但后续持久模型输入把序列化结果截取至 4096 字符，完整摘要丢失。下一调用因无效摘要失败，没有创建 Revision 或执行实验。历史失败及用量保留，不能补造丢失的原始参数。

累计已核实 32 请求、432017 tokens，0 未结用量。原授权调整为累计 400000000 tokens，60 请求；历史消耗不重置。工程修复期间不调用付费模型。

## 机制与使用边界

工具结果保留完整关键对象、版本、审批摘要、预算和 Observation；仅长源码、差异、日志等详情字段转为同会话回执引用。`context.tool_result` 经审批按 JSON Pointer 分页读取已完成回执，不接受任意文件路径。分页是来源数据片段，不是语义摘要，也不提升为系统指令。

正常完成、用量已核实的非法 JSON、未知工具和字段错误登记为 `invalid`，`executed=false`；与有效提案同事务保存。参数不会被替换成空对象执行。有效写提案仍须审批，全部提案终态后才续推理。每根用户 Turn 最多两次参数纠正，共用八层链与会话、总账本限制。输出达到长度限制且参数不完整时停止，保留已核实用量，不进行自动纠正；断流、未知用量、取消和已消费确认同样不自动重发。

引用式 `research.revision_commit` 和 `research.revision_run` 分别引用同项目、同会话、同 Study 的已完成预览/创建回执；仍需创建与执行两次独立审批。源码、版本和摘要从权威回执读取并重新核验。旧完整参数接口保留。

## 验收状态

实施中，尚未完成最终全量回归、冻结新执行修订、读拒绝探针和真实运行接续。不能使用此前修订 8 的回归替代当前代码验收。新增本地 OpenAI 兼容 HTTP/SSE 集成测试用于核对无效参数、完整消息组和真实用量结算；不存在付费调用。

后续记录必须区分专项、前端、资源一致性、完整回归、模拟验证、浏览器实际操作和跳过项。工程通过不等于研究改善；没有真实实验反馈链及确认结果，不报告自主科研验证完成。

### 2026-10-04 实际接续暴露的 Windows 路径故障

修订 10 启用后，真实请求 33 提出了引用式创建调用 `tcl_umbkoy7aru7b35dlypx4iluu6y`。单独审核创建后，写入 prepared 回执的原子临时文件达到 260 字符，引发 FileNotFoundError；调用顺序确认失败发生在 submit_initial_revision 之前，不产生新 Revision 或实验执行。失败及用量证据保留，不重写成成功。原模型根据真实失败自动续推理，请求 34 提出 `tcl_muw3akrelt5q3h2tsz65hgxxdu`，该提案暂不批准。

累计实际消耗 34 次请求、469763 tokens，0 未结用量，剩余 26 次请求；不重置历史消耗。修复仅移除回执文件名中的冗余 reference- 前缀，保留完整 SHA-256 和回执身份验证；旧文件继续可读，prepared 恢复写入新短路径，旧文件不删除。Research Kernel、科学材料、模型数值源码及单次组不变。

新增真实长度原子写入、旧 prepared 回执恢复和身份冲突拒绝测试；引用式工具/精确编辑/SSE 专项 25 passed、6 subtests。测试初版长度断言差一字符已纠正，失败不当作通过。执行修订 11 已冻结，SHA-256 `b566413346d8165ffc70b2725d210ac3c76b6eed355e0a3f4714cf85ba55fb4b`，尚未启用；须完成此修订独立全量回归和实际读拒绝探针后才接续审批。

修订 11 第一轮完整回归日志停在 73%，进程不再存在，且没有终局测试统计或可查询退出码，按中断处理，不算通过。原日志 `acceptance/full-regression-execution-layer-rev11-2026-10-04.log` 保留。2026-10-04 13:15（北京时间）启动唯一新的持久串行验收，日志为 `acceptance/full-regression-execution-layer-rev11-persistent-2026-10-04.log`；辅助入口 `evaluation/run_frozen_regression.py` 先校验冻结源码，执行 CI 命令，结束时记录日志摘要、真实退出码及前后源码一致性到同名 `.receipt.json`。它不运行模型、不执行正式研究、不自动启用修订。未产生结束回执前仍不能报告全量通过。

持久回归已于 2026-10-04 13:27（北京时间）结束：1220 passed、5 skipped、630 subtests passed，685.35 秒，真实退出码 0，sources_stable=true。原日志 SHA-256 `199b166baa2a03c2fa65362adfb4e9f0633a5163d2e3e3fd42d369da970bf61f` 和退出回执保留。Windows 子进程采用 CP936 输出，因此另生成只做编码转换的 `acceptance/full-regression-execution-layer-rev11-persistent-2026-10-04-utf8.log`，SHA-256 `e412f91459d414d804ee3d31f22b979183e48b5eee9d16c4670fe7eeb531f408`，不覆盖原件。跳过项为 TTC 集合字体、Windows symlink 权限、两条与本平台可用后端相反的分支、Linux bubblewrap 专属分支；不当作已验证。

修订 11 随后通过原启用入口生成 `execution-arms/0011.json`，对应同一冻结摘要与 UTF-8 日志摘要；实际读拒绝探针通过、宿主读取验证通过。Windows 无内核级断网的限制仍保留。此时累计请求为 34，未增加模型请求；后续只接续已有原生创建提案，创建和执行独立审核。

### 修订 11 的真实模型反馈链与负结果

创建调用 `tcl_muw3akrelt5q3h2tsz65hgxxdu` 单独审核后完成，产生 Agent 作者 Revision `REV-6e490545d1dfbb8eb4ea1284`；manifest SHA-256 `e8819e23158adae9f1df58e58bb22be57f842a9f9efb9c2ffd4ffe086c5f933d`，替换源码摘要与原预览一致，没有人工数值修改。模型请求 35 随后提出 `research.revision_run`，调用 `tcl_nlmav6dfgmhnok3s7stmggwwzm` 经独立执行审核，入队 Job `JOBR-4a11eb676145992ec884afac`。入队回执明确 `enqueued_not_completed`，不当作实验完成。

实际 Job 于 13:33:46 进入 succeeded，产生 Run `RUN-515e23ebf0a80ad22eb82e98` 和开发 Observation `OBS-c803c4345c2c2606de`：trajectory_error（min）为 `62884.99990291138`，远高于固定 RK4 开发值 `0.15025162534812272`；改善量为 `-62884.84965128604`，不满足冻结阈值 `0.030050325069624545`。这是有效的执行与评分负结果，不是确认集结果，也不表示数值方法成功。

模型请求 36 提出了 Job 查询 `tcl_zmrqp3yzymicutbrfugv32pz2m`，只读审核后将真实 Observation 与可信计量回执送入续推理。请求 37 完成并决定停止该 Revision 路线，未提出新工具。由此保留了“原模型预测/源码 → 独立创建审批 → 独立执行审批 → 真实实验 → Observation → 模型停止”的完整链。原预测与原解释不追补修改，所有 native-development-approvals 回执及持久输入快照保留；工程反馈链通过不等于科研增益通过。

模型停止解释还存在明确缺陷：将完成案例称为 passed、开发次数记为 3/6（实际候选执行 4 次）、没有证明却泛化为 RK4 接近最优和整类方法/假设被反驳。它们均作为未验证的 Agent 解释保留，不采用为报告科学结论。Job 结果旧结构只有 executions/job/observations/study_id，缺少最新状态版本和开发预算；将补齐只读字段以降低沿用过期上下文的风险，不借此重跑模型或重写历史回答。独立确认未消费，未产生 Claim。

最终只读核对：累计 37 次请求、527308 tokens，0 未结用量；没有待审批/已批准未执行/运行中工具或模型 Job。Study 版本 45，候选开发已用 4/6、剩余 2，Kernel 家族预算 cap=9/spent=5/reserved=0/available=4（控制与候选均计入，不与候选次数混淆）。真实 Worker `JOB-f9d8d20b41ac0c7ce786dd20` 的 RHS 回执摘要为 `3807d530c0f4e01000d42f32d23da5f28020813e7e6da73ec4332fcc55800dc0`；32 个案例均完成，查询数均等于各自额度，总计 327680 次/327680 次额度。这里仅验证受控 RHS 查询计量，不能称为全部算术成本相等。

模型请求 37 的持久输入摘要 `ba217c1212ed696c0ea3a63d6206c41020e662ffabcb8fd66cd34503977f5afd` 仍包含历史版本 40 和次数 3 的消息；当次 Job 工具结果没有更新到版本 45/次数 4 的字段。历史事实不得补写，下一修订仅增加权威只读版本与预算投影，并测试必需模型字段完整保留。

### 修订 12：最新版本与预算上下文

新 Job 查询增加 source-provided state_version、Kernel budget 和适用的 candidate_development_budget，Observation 方向仅在权威 manifest 提供时返回。候选额度来自既有冻结 evaluation-policy.json，按现有执行前检查的口径统计同 Study 队列中不同的非控制 Revision，包含失败、取消、入队和运行中，重复 Revision 不重复计数。缺少队列时只报告 0，不创建数据库；冻结输入变化、队列完整性失败或不可验证 Revision 均拒绝。没有新增科研状态存储、修改 Kernel 或修补旧模型回答。

相关后端串行验证 44 passed、18 subtests passed；新元数据专项 8 passed、4 subtests passed。前端重新验证 19 文件/123 项测试通过，构建退出 0，Monaco/JSON 大块警告原样保留；20 个暂存文件与新构建一致，无须重新暂存。修订 12 摘要 `47b258da13dc1677b8c08751ff66864237f05df9b221cdc884f935f0cf9dcfc7` 已冻结，完整验收日志为 `acceptance/full-regression-execution-layer-rev12-persistent-2026-10-04.log`，真实退出回执在同名 `.receipt.json`。未结束前不报告通过，也不发起新模型请求；修订 11 的真实负结果仍明确归属修订 11。

修订 12 最终串行全量回归于 2026-10-04 14:03（北京时间）结束：1228 passed、5 skipped、634 subtests passed，677.08 秒，退出码 0，sources_stable=true；日志 SHA-256 `2ed1c32a70c634a7f55fadc8b521639604b952db6a869b2809f65df8c725afa4`。本次输出为 UTF-8，无须覆盖或转码；5 个跳过项与修订 11 相同，仍未宣称跨平台全部验证。原启用入口随后完成本修订实际探针，`execution-arms/0012.json` 核对同一源码及日志：47 个受保护文件候选读拒绝、host_read_verified=true、network_block=false。

实际保留项目的只读元数据投影确认 4/6、剩余 2，Study 版本 45，Kernel cap=9/spent=5/available=4，最新 Observation 的方向为 min；148 个持久项目/研究文件摘要前后全部相同，未新增或删改文件（WAL/SHM 不作持久证据）。修订 12 没有新付费模型调用，没有重跑已停止的候选，也没有把新元数据补进旧输入快照。累计仍为 37 次/527308 tokens，0 未结用量；无待执行/审批工具或运行中模型 Job，独立确认未消费。

最终边界：本机执行层机制及上述工程验收通过，真实反馈链在修订 11 跑通，修订 12 的补充元数据经模拟链与实际只读投影验证。科研增益不通过，不能称为自主科研能力全面通过或原创发现；Agent 理论解释仍待人工审校。未新增 UI 仪表盘、SDK、公开发布或团队能力，保留已有数据与历史失败。正常服务未贸然重启以避免恢复其他项目付费队列；临时验收服务已结束，浏览器视口在此前验收后恢复。

### 已完成的专项与浏览器复查

- 前端全量：19 个文件、123 项测试通过；最终构建退出 0。原有 Monaco/JSON 大块资源警告保留，没有隐藏。20 个暂存资源与构建一致。
- 后端相关测试第一组：104 passed、8 subtests，覆盖会话 HTTP、原生 Revision、SSE 参数纠正、工具详情范围、传输失败用量及上下文。
- 引用/精确编辑/执行队列及完整 SSE 专项：26 passed、6 subtests。
- 修复过期续推理提示后的会话/引用/完整 SSE 专项：58 passed、2 subtests。以上集合有重叠，不能相加当作独立测试总数。
- 本地 OpenAI 兼容 HTTP/SSE 六请求链实际通过：无效审批摘要未执行 → 校验反馈 → 预览 → 单独创建审批 → 单独执行审批 → Job → Observation → 结果续推理。使用隔离二次回归工程样例和模拟模型，不是正式振荡系统结果，也不是模型研究增益。
- 实际工作台只读查看上述隔离会话：无效调用没有审批/执行按钮，创建/执行分别说明，入队回执不冒充完成；真实 Observation 可打开右侧标签。输入草稿在对象跳转和窄窗口“对话/工作区”切换后保留。
- 浏览器曾发现后续 Turn 已结束却显示旧续推理按钮，已修复为仅展示当前 Turn 的协调状态并增加回归断言。修订 9 的全量回归因此人工中断，退出码 -1；原日志保留为 `acceptance/full-regression-execution-layer-2026-10-03.log`，不当作通过证据。

修订 10 SHA-256 为 `e04cc1b2e522548aa812194dac75c49a6986e5190e49454a850b084cf7f435fc`，已冻结但未启用。其最终串行全量回归日志为 `acceptance/full-regression-execution-layer-final-2026-10-03.log`；最终退出码和结果尚待核对。此前修订 8 的通过记录不替代它。

正常 5173 页面在本轮浏览器检查时连接被拒绝，未贸然启动可能恢复付费队列的服务。隔离 8768 验收服务启动前只读核对 6 个模型 Job 全为 succeeded、工具为 4 completed/1 invalid，无待执行队列；浏览器操作没有发送消息或重新执行实验。截图为 `acceptance/execution-layer-observation-1440.png`、`acceptance/execution-layer-chat-760.png`。截图中的历史默认模型标签不能作为远程模型调用证据，实际请求全部来自本地模拟服务。

### 2026-10-04 全量结果与接续

修订 10 第一次完整回归已结束：1 failed、1216 passed、5 skipped、630 subtests，1140.41 秒，退出码 1。失败名称为 `CampaignDispatchTests.test_invalid_tool_result_still_records_actual_provider_usage`，其旧断言要求未知工具令整个 Turn failed，而本方案明确要求已核实用量的调用登记 invalid、未执行，Turn 保留提案与反馈。已更新该断言并补核对 invalid、无结果、不可批准及不可执行；实际已用 31 tokens 的断言保留，专项 6 项及 2 subtests 通过。没有隐藏原日志，也没有修改运行源码或科学规则。

当前重新运行相同 CI 全量命令，日志 `acceptance/full-regression-execution-layer-verified-2026-10-04.log`。修订 10 的实现摘要未变化，仍未启用；必须核对新的最终退出码后再执行 arm/实际读拒绝探针及付费接续。1440、760 及 1024 刷新截图已保存，视口已恢复，隔离服务已结束。浅色主题、完整键盘/焦点覆盖本次未重新跑浏览器，前端自动测试与旧验收仍各自独立，不能冒充本轮实际检查。

第二次完整回归已结束：1217 passed、5 skipped、630 subtests passed，1402.28 秒，最终退出码 0；日志 SHA-256 为 `7d3d5959d56b72b7d67cb9b81b72f29bdde65d255c420c6149c98505efab058b`。修订 10 已通过现有 arm 入口启用，47 个实际受保护文件候选读拒绝，宿主读取正常；Windows 仍无内核级断网，不能称完全封闭。资源一致性再次核对 20 文件匹配。

现在允许按新执行配置接续第二闭环，保留前 32 次/432017 tokens 的消耗以及旧失败。接续入口只提供已完成预览的真实回执身份及真实错误，不补造丢失参数，不提供数值方法或源码修改；创建、执行仍分别审核。接续派发或提案不代表实验完成，更不代表科学增益。
