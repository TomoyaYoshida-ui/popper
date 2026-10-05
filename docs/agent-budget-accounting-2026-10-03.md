# 2026-10-03 执行预算结算修复

## 变更

单次预留是估计，不是独立费用授权上限。已核实的实际用量超过预留时，保留 `usage_over_reservation`、原输入快照、预留数和实际用量，但不再把它视为未知用量。下一次请求仍以真实累计用量和新预留检查原有 60 次请求、400000 tokens 总上限。

已结算的超预估记录不可被重写为其他用量或未知状态。重复请求不重新发送。未知用量、在途请求及总额度不足仍阻止调用。旧数据库无需迁移或修改记录。

## 验证

命令：`python -m pytest -q tests/test_agent_evaluation_budget.py tests/test_chat_provider_limits.py tests/test_provider_accounting_failure.py tests/test_campaign_dispatch.py tests/test_oscillator_campaign.py -rfEs --tb=short`

结果：45 passed，5 subtests passed，15.06 秒。

覆盖实际用量结算、超预估重启恢复、不可变结算、总额超限、未知用量、模型限制快照及调用去重。全部使用本地测试，不新增付费调用。

当前源码的全量回归、执行版本重新冻结及正式模型实验尚未完成；此前回归通过不能替代本次验收。科研独立确认仍需单独审批。

## 执行修订 4

后续已冻结修订 4，SHA256 为 `ca4e0c447d8bfa41340b20bf3380541ffa8a99f10271755ede242d7e23299e1d`，尚未启用。真实账本只读核对为 8 次请求、122429 tokens、0 未结清请求、1 超预估记录、1 聚合核对但未分配单次用量记录。没有改写旧预留或供应商用量证据，没有重发旧运行。

冻结后的 CI 回归安排在当前测试进程结束后串行执行，输出为 `evaluation/runs/agent-oscillator-2026-10-01-v3/acceptance/full-regression-frozen-budget-2026-10-03.log`，退出码另存同名 `.exit.txt`。只有日志完整通过、退出码为零且源码摘要不变，才能进入实际读隔离探针与启用。此处不预先宣称通过。

## 新完整对照组准备

按用户确认，新建 `formal-campaign-qwen-v2`，六个独立项目为三次单次组、三次闭环组。清单摘要 `a0bfa48291a4013ad4a9cc774bc313087edace31bd6b3edb13e23343cede589a`；原 v3 协议和材料保持不变，账本仍为原 `formal-campaign-v1/budget.sqlite`。旧 MiMo 和 Qwen 失败分别保留，不替换、不混算新组的有效对照。实际模型限制另记于 `cohort-supplement-v1.json`，仍绑定源码和每次持久输入快照。

目前仅完成离线准备，尚未启用、没有新增付费请求，没有创建 Study 或执行候选。准备后的完整回归与读隔离仍待完成。

后续第一轮完整回归实际结束：1140 passed、5 skipped、614 subtests passed，673.63 秒，退出码 0。该轮在修订 4 冻结前启动，不作为新组启用的最终日志。前端测试实际通过 111 项、19 个文件，19.31 秒。新组准备后的串行回归已于 2026-10-03 13:21 启动，尚无最终结果。原始 PowerShell 输出可能为 UTF-16；若需规范编码，保留原文件并生成独立 UTF-8 副本，不覆盖原验收记录。

## 最终回归与启用回执

冻结后串行回归实际结束：1140 passed、5 skipped、614 subtests passed，656.86 秒，退出码文件为 0。原日志 SHA256 `246a4f78c818d47932546e8109fcb59ac9c79bf759fad845897f8034ef74cc37`；另存的 UTF-8 副本 SHA256 `6603ed528db0233bf0dbfeaf3134977d1fbeff692dff3fff5ee38ce38f3ad63e`，未覆盖原件。输出的中文跳过说明存在编码损坏，保留原貌，不虚构修复后的说明；五项跳过涉及 TTC 字体、符号链接权限、两项不可用后端及 Linux bubblewrap。

新组通过原 `arm` 入口启用，回执位于 `formal-campaign-qwen-v2/armed.json`，绑定新组清单和完整回归副本。47 个受保护文件的实际候选读拒绝与宿主读取均通过；无内核级断网，不宣称完全封闭。启用时共享账本仍为 8 次请求，仅授权初始模型提案，不授权 Study、Revision、执行或确认。

随后仅启动新组 `single_shot-1` 的真实提案请求，结果仍需等待真实终态。其他五次未启动，旧失败不重发。

## 首个真实回答终态

新组首个 Turn `trn_2fdc5w2o5igz6o7ugwx3blokhi` 已完成，供应商回报输入 6088、输出 2417、总计 8505 tokens。账本累计 9 次尝试、130934 tokens、0 未结清请求。正常返回与计量已验证；没有声明货币费用为零，接口未报告可核实的货币费用。

首个预览暴露旧检查将 `aggregate_reconciled` 和已知超预估错误当作未结清的问题。已修复只读预览检查，26 项专项测试、2 子测试通过。新组执行修订 1 摘要 `c65e9023cada0270ba8bb8587de94313492c64194579bcb842ad9c8943173e1a`，尚未重新启用；此前完整回归属于修复之前，不能替代当前源码回归。

离线重核原回答仍因多出的顶层 `initial_prediction` 被严格格式拒绝。模型确实提供六个假设、选择 Velocity Verlet 并给出完整代码，但未修改或删减回答以伪造通过；原 `initial-answer.json` 保留，追加 `offline-revalidation-v1.json` 区分最初适配错误和实际格式失败。初始提示要求包含预测但未明确其顶层禁用，这属于提示与格式契约的可用性缺口，不能据此断言模型科研能力低。源码还存在预算计数风险：一次初始 RHS 查询加每步查询可能超过它声明的预算；仅为静态观察，未执行或改变方法。

没有创建 Study、Revision、Observation 或 Claim，没有消费独立确认。其他五个新组模型请求未发送；不自动修补、重发或放宽格式。真实实验能力仍未验证。

## 用户批准的输出契约修订

上述格式失败之后，用户明确同意将 `initial_prediction` 定义为可选文本字段并离线重核已有回答。现在只接受非空、最多 4096 UTF-8 字节的文本；其他未知字段、凭据、对象范围变化及未结清用量仍拒绝。预测保持来源数据身份，不成为已验证结果或批准权限。存在该字段时将其完整纳入预览和审批摘要；旧的不带字段的预览仍兼容。未来新准备的提示明确允许字段和假设字段类型，既有冻结提示不修改。

修订后专项命令 `python -m pytest -q tests/test_oscillator_proposals.py tests/test_oscillator_campaign.py tests/test_agent_evaluation_budget.py -rfEs --tb=short` 实际通过 41 项，10.98 秒。上一轮（账本预览修复后、此次契约修订前）完整回归实际通过 1141 项、5 跳过、614 子测试，654.28 秒，退出码 0；不能替代最新契约源码回归。

追加 `single_shot-1/offline-revalidation-v2.json`：仅在修订契约下通过，明确 `valid_under_original_contract=false`，原始回答、最初 `initial-answer.json`、重核 v1 失败均保留。没有追加模型调用。来源摘要仍为 `26260f5cc26d5122239557977e2c2c70be2d1dd1719ffc3048bfefe94c113197`；新 Study 审批摘要为 `b2f248769bdcb33e187c2a09f321c01e3c0f176e5930c2065f48cf22827d59b4`。

该批准只能创建绑定原作者六个假设的 Study，不创建 Revision、不运行代码、不消费确认。实际方法预测尚未验证。代码的潜在初始 RHS 超额和文中方法阶数表述均保留为待核验风险，不进行人工研究救援。

## 最新契约源码最终验收

2026-10-03 复核最新完整回归实际通过：1146 passed、5 skipped、614 subtests passed，659.14 秒，退出码 0。日志 `acceptance/full-regression-prediction-contract-2026-10-03.log` SHA256 `ae68e7be2ca4599bb6e377c914933a035b532a36db7bc758ea42c75f06af18fd`。新组执行修订 2 SHA256 `08bdc737fba0a657c6ba2be3841e62945ad3ba3019fdced7d20d8c6cf7662965` 已通过现有 arm 入口启用，回执 `formal-campaign-qwen-v2/execution-arms/0002.json`。

47 个指定受保护文件的候选读拒绝与宿主读取再次通过，仍无内核级断网。共享账本保持 9 次请求、130934 tokens；没有新增模型调用。离线预览与已保存的修订契约预览完全一致，审批摘要不变，研究目录尚未创建。

此处完成的是契约修复与执行准备验收，不是科研能力验证。下一步等待明确批准创建 `single_shot-1` 的 Study；用户批准契约修订不自动等于批准创建 Study，更不等于批准候选执行或独立确认。原格式失败继续保留并在正式报告中披露。

## 用户批准创建 Study

用户明确回复“批准创建 Study”后，使用已核对摘要 `b2f248769bdcb33e187c2a09f321c01e3c0f176e5930c2065f48cf22827d59b4` 调用原批准入口，创建 Study `S-ee28f70e36c543bc`。研究问题和六个假设绑定原真实 Turn 与完整模型回答，原失败记录仍保留。

只读生成初始代码差异预览并保存为 `single_shot-1/initial-code-preview-v1.json`。拟创建 Revision 对应假设 `H-31c761a59c6148ca`、设计 `D-31c761a59c6148ca`、状态版本 22；提案摘要 `a38c5c71e9b3973ed99055f16f7e5cd5640d17c5a7b2d072d3a674634bd6345b`。代码仍是 Agent 原始 Velocity Verlet 实现，保留静态预算风险，不由实施者修补。

Study 创建不授权 Revision 创建或执行。当前没有候选 Observation 或 Claim，没有新增模型调用，没有消费确认资源；共享总用量仍为 130934 tokens。下一步等待用户核对源码差异后明确批准仅创建不可变 Revision。

## 用户批准创建 Revision

用户明确回复“批准创建 Revision”后，按提案摘要 `a38c5c71e9b3973ed99055f16f7e5cd5640d17c5a7b2d072d3a674634bd6345b` 创建不可变 Revision `REV-85d9a3da5c178d4d211fd744`，清单摘要 `79cfba7d64fb8d3e0ed7bb225bb78544bfae290aa3a22712019b5671082718cc`。作者验证为 Agent，源码与原模型回答一致，冻结项目 `model.py` 不覆盖。回执为 `single_shot-1/first-revision-binding.json`。

随后仅通过现有应用服务生成只读开发执行预览，保存为 `single_shot-1/initial-execution-preview-v1.json`。Study 版本 22、Revision 摘要与归属校验均通过；开发预算 cap 9、spent 0、reserved 0、available 9。执行将入队开发 Job，失败也消耗开发预算。预览期间明确禁用模型调用，并先核对该项目现有 Agent Job 已为 succeeded，无在途模型请求。

执行尚未批准和启动，没有新的 Observation 或 Claim，没有消费独立确认。原代码的初始查询加逐步查询可能超出每案例 RHS 查询上限，风险在执行预览中明确披露；不擅自修复 Agent 的方法实现。下一步等待单独批准开发实验执行，不以 Revision 创建批准替代。

## 用户确认后的真实开发执行

用户在澄清引用不等于批准后明确回复“确认”。按原执行预览的 Study 版本 22、不可变 Revision 清单摘要，通过现有应用服务执行一次开发实验。稳定调用标识为 `human-approved-qwen-v2-single-shot-1-development-v1`，保留应用层回执，不制造 Agent 工具提案。Revision Job `JOBR-540259caed68bd67432188c1` 在 2026-10-03 15:02:03 入队，15:02:06 到达 failed 终态；Worker Job 为 `JOB-f5b474e7ef379a6ab4727ccd`。

真实宿主 RHS 收据确认：首案例 `dev-harmonic-0` 成功计量 4096 次查询，上限 4096，下一次请求被拒绝；stderr 为 `RuntimeError: RHS query budget exhausted`。候选确实运行，但异常导致 trace incomplete，上层显示 `implementation_failed/UnexecutedRevision`。不能把这个上层标签解释为代码从未执行，也不能仅看标签抹去实际超预算原因。其余 31 个案例为 not_completed、查询数 0，保留失败现场，不删除困难案例。

开发预算 spent 1、available 8、reserved 0。没有 Observation 或 Claim，不能虚构轨迹误差、能量漂移或成功率评分。总模型账本保持 9 次请求、130934 tokens，执行期间没有模型调用，没有消费独立确认。完整结果及收据摘要保存于 `single_shot-1/development-result-v1.json`。

本运行属于单次组，按原规则不得根据开发反馈修补其代码或重新调用模型救援；本次保留为真实负结果。闭环组的反馈修正能力尚未验证，其他五个新组提案仍未发送。要验证该能力，应推进独立的闭环组，而不是把单次组事后转换为闭环。

## 独立闭环组初始提案

用户继续推进后，核对当前已启用源码、账本与未尝试状态，仅发送 `popper_loop-1` 的初始请求，不向其提供单次组的任何开发反馈。真实 Turn `trn_23m2ch6mkoe55hh6ez4v4e66gq` 正常完成，输入 6097、输出 2713、总计 8810 tokens；账本累计 10 次请求、139744 tokens、0 未结清请求。

模型提供完整 JSON、六个假设、初始实现与结构化初始预测，但 `initial_prediction` 为对象，当前明确的可选文本契约拒绝它。对象中的误差、能量漂移、失败率均为模型预测，不是实验结果。原冻结提示未限定该字段类型，未来新准备提示虽已补齐类型，既有冻结提示未修改。因此这里再次暴露输入提示与解析契约不一致，不能据此断言闭环科研能力不足。

保留 `initial-answer.json` 原失败、持久原回答和追加 `proposal-contract-failure-v1.json`。不将对象偷偷转换成字符串，不改答案、不重发。闭环 Study 尚未创建，也尚未执行；其他四个新组初始请求未发送。扩大字段类型属于新的契约修订，须用户明确同意并在报告中披露，不能把修订后通过伪装成原规则下通过。

## 用户批准 JSON 预测对象

用户明确允许“文本或有大小限制的 JSON 预测对象”。现已接受非空文本或非空对象，最多 4096 UTF-8 字节（对象按紧凑 JSON 序列化计量），拒绝非有限数字、顶层数组、空对象及其他未知顶层字段。JSON 预测原样保留并纳入审批摘要，不解释为系统指令或执行批准；未来新准备提示同步明确字段类型，旧冻结提示不修改。

专项回归实际通过 45 项，11.46 秒。没有新增模型调用，未创建闭环 Study。离线核验仍被目标/候选严格相等检查拒绝：模型目标只删去了 `固定 RHS 查询预算` 中的空格；六个 slot 仍为 1..6，但原候选 `{slot}` 中增加了 method 与 description。该差异与预测字段修订无关，不能隐式覆盖预注册输入或声明整份提案已通过。原答案与失败记录继续保留。

后续需要明确候选槽位身份与作者说明的界限，以及目标文本空白差异的展示/核验规则。实现者尚未放宽这部分规则，也没有把不一致的提案用于创建 Study。

## 失败结果进入闭环工具查询

继续目标的当前轮确认了一个应用层缺口：失败 Revision Job 的 `run_id` 为空，原工具因此返回空 executions，即使可信 Worker 收据已经存在。现已在只读查询中使用 Controller 的确定性开发 Run 标识，并要求权威 Run 同时匹配 Revision、设计与清单摘要；Worker 也必须匹配相同归属、开发幂等前缀和终态。Observation 按精确 Run 过滤，避免失败父版本误读后来成功子版本的结果。此变更不修改 Kernel 或既有失败、预算、审批记录。

专项 `tests/test_agent_revision_tools.py tests/test_oscillator_worker.py tests/test_revision_execution_jobs.py` 通过 12 项，11.91 秒，包含失败反馈与父子 Observation 隔离。原真实失败 Job 的只读服务查询验证通过，实际返回一个 Worker 执行、32 个案例及带 SHA256 的有限日志摘录，确认 RHS query budget exhausted。保存 `single_shot-1/development-tool-evidence-v2.json`，不调用模型、不重跑、不把失败变为 Observation。

当前目标继续 active；完整研究验证未完成。目标/候选说明的接纳规则尚待明确核对，不能凭持续目标绕过该审核或后续独立确认审批。最新源码将重新冻结并执行完整回归，旧全量通过不冒充当前源码验收。
# 执行修订 3 验收终态补记

用户再次明确允许文本或有大小限制的 JSON 预测对象；实现仍限定非空文本/对象、4096 UTF-8 字节、有限且可序列化 JSON，保留原回答与审批摘要。此许可不扩大到候选槽位说明或目标空白差异。

既有完整回归已结束，1150 passed、5 skipped、614 subtests passed，679.02 秒，退出码 0；原日志 SHA256 `3aa30af8336fc336dcb483caaf1aebd2f57cc9fd5a2bc273d8e44567f9657bee`。执行修订 3 重新启用成功，47 个受保护文件读拒绝探针及宿主读取通过；Windows 无内核级断网。仅启用初始模型提案范围，没有新模型请求、Study、Revision 或实验执行。闭环提案的预注册归属校验仍待明确接纳规则，不报告科研验证完成。
