# 自主对照试点复验（2026-10-10）

## 停点与续作

Codex 在 `pilot-repaired` 启动后因额度用尽中断。续作时发现该轮已跑完：
线程修复生效，但模型单元仍大量失败；其中 `P02-generic_iteration`
已有确认结果，却被后续 `stream_interrupted` 记成开发失败。

## 本次修复

1. `UnexpectedModelBehavior` 映射为 `action_contract_rejected`，保留流式回执。
2. 研究循环对非传输类契约拒绝继续下一步，不再整单元中止。
3. 传输中断后若已有 finalist，开发状态记为 `development_completed`，错误保留在记录中。

专项回归：Windows 64 项、Linux 67 项通过。

## `pilot-retry` 结果

额外生成任务、六单元、同一总账本。不进入正式 72 单元确认数据。

| 策略 | 确认完成 | 支持 | 说明 |
|---|---|---|---|
| fixed_queue | 2/2 | 0/2 | 工程基线正常；科学判断 inconclusive / refuted |
| generic_iteration | 2/2 | 0/2 | 模型出题、实验、确认已跑通；含契约拒绝与代码失败后仍选出 finalist |
| evidence_driven | 0/2 | 0/2 | 首包即 `stream_interrupted`，用量未知并按协议停止 |

全局账本（含此前 pilot / pilot-repaired）：73 次发送记账、约 47 万 known token，
未解析预留约 16.8 万 token；总上限 1600 / 1800 万仍充足。

## 结论边界

- 线程跨访问与软契约拒绝不再阻塞普通迭代闭环。
- 尚不能比较三种策略优劣：证据驱动未完成开发。
- 未显示策略优势；不启动正式 72 单元，也不作能力声明。

## 串行复验 `pilot-serial2`

证据驱动首包中断来自两个单元同时打开提供商流。模型调用改为同一把锁串行，实验执行仍保持并发 2。

| 策略 | 确认完成 | 支持 |
|---|---|---|
| fixed_queue | 2/2 | 0/2 |
| generic_iteration | 2/2 | 0/2 |
| evidence_driven | 1/2 | 0/2 |

`P01-evidence_driven` 完成 5 次实验并确认，科学判断为 inconclusive，开发因单元发送上限停止。
`P02-evidence_driven` 在一次已结算请求用尽两次发送后被整段中止，没有成功候选。
该类别已改为跳过该决策并继续，不再当作传输中断。

## 跳过逻辑冻结与 `pilot-skip`

专项回归只覆盖运行时、基准、规划器、基础层、声明投影，以及 Linux 沙箱。Windows 66 项通过，Linux 69 项通过。旧的全量回执仍是改动前基线，没有重跑，也没有调用 `finalize_gates.py`。活动门禁追加了这两份专项回执，并写明源码指纹对应“耗尽的逻辑请求只跳过该决策”。指纹共 203 个文件，`pilot-skip` 的 `cohort.json` 已绑定到同一指纹。已有暴露账本、`pilot-serial2` 和 `formal` 的登记数据都没有覆盖。

`pilot-skip` 为 2 个新生成任务、6 个单元、1 次重复。研究、试点和盲评仍写入同一 `model-calls.sqlite`。模型调用串行，实验并发为 2。运行退出码为 0。

| 策略 | 确认完成 | 支持 |
|---|---|---|
| fixed_queue | 2/2 | 1/2 |
| generic_iteration | 2/2 | 0/2 |
| evidence_driven | 1/2 | 1/2 |

证据驱动通过门禁：至少 1 个单元 `confirmation_completed`。`P01-evidence_driven` 完成确认，科学判断为 supported；开发在已有候选后因单元发送上限停止，状态仍是 `development_completed`。`P02-evidence_driven` 在单元发送上限处停止，确认状态为 `not_evaluated`，保留在分母中。本轮没有出现把 `attempt_budget_exhausted` 当成整段失败的情况。`pilot-skip` 各请求范围均已结算，没有新增未知用量。

试点结束后的共享账本：已发送 217 / 1600 次，已知 token 1,807,658 / 18,000,000，未结算预留 182,482（来自更早的未知用量记录，不是这次试点）。支持不等于策略优势。本轮不根据试点比较三种策略。

## 正式 72 单元

门禁通过后，已准备好的 `formal`（12 任务、72 单元）执行了 `bind-code`，绑定到同一源码指纹，并完成开发、冻结、盲评和确认。`report.json` 已写出。失败留在分母中，并与科学反驳分开记账。

| 策略 | 确认完成 | 支持 | 计划 |
|---|---|---|---|
| fixed_queue | 24/24 | 3/24 | 24 |
| generic_iteration | 21/24 | 1/24 | 24 |
| evidence_driven | 21/24 | 2/24 | 24 |

开发：69 个 `development_completed`，3 个 `failed`。确认：66 个 `confirmation_completed`，4 个 `not_evaluated`，2 个 `failed`。科学判断：supported 6，refuted 21，inconclusive 39，not_evaluated 6。支持不等于策略优势，本轮不宣称三种策略孰优孰劣。

共享账本在正式对照结束后：已发送 1066 / 1600 次，已知 token 10,174,742 / 18,000,000，未结算预留 266,745（含更早未知用量与正式队列中的读超时等记录）。正式队列内结算 847 次，未知用量 2 次，未发送 1 次；未知用量按协议未重放。

描述性基线与分母拆解见 `adaptive-formal-baseline-2026-10-11.md`。根因治理（拒绝审计、预算语义、确认预检）见 `adaptive-root-cause-governance-2026-10-11.md`。
