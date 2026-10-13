# pilot-revision-capacity 描述性综合（2026-10-13）

数据源：`/home/research-runner/adaptive-v2-validation/campaign/pilot-revision-capacity/report.json`。  
声明：`descriptive first cohort; no general superiority claim`。  
比较单位是 **task**；重复不是独立数据集。失败留在分母。  
本文件只描述工程门禁在 live 小样本上的表现，不宣称三种策略优劣，不声称通用优势。

本队列是修订容量指纹下的试点：两个 `iid_row` 生成任务（PR01、PR02），每种策略一次重复，共 6 单元。  
模型 `qwen3.8-flash`。与已锁定的 formal / formal-independent-72 / formal-root / formal-iid-72 分开记账。  
`formal-generated-root` 仍未启动。

## 1. 完成率与科学状态

| 策略 | 确认完成 | supported | planned |
|---|---|---|---|
| fixed_queue | 2/2 | 0/2 | 2 |
| generic_iteration | 2/2 | 0/2 | 2 |
| evidence_driven | 2/2 | 0/2 | 2 |

合计确认完成 6/6。  
科学判断：supported 0，refuted 1，inconclusive 5，not_evaluated 0。

工程终态：6 格均为开发完成且确认完成；选择阶段均有 finalist，无 `empty_finalist_reason`。

## 2. 相对 formal-iid-72 的工程对照（描述性）

`formal-iid-72` 有 5 格因修订抬高版本后未复验而 finalist 为空，落成 `not_evaluated`。  
本试点 6/6 均有 finalist 并完成确认，**未复现**该工程失败模式。

这不是策略恢复证明，只说明在修订容量指纹与本试点规模下，分母里没有出现「修订后无复验 → 未评」。

## 3. 修订与软拒绝收据

| 类别 | 数 | 说明 |
|---|---|---|
| 已应用 `revise_hypothesis` | 2 | `PR02-generic_iteration-r0`，`PR02-evidence_driven-r0` |
| 修订后复验尝试 | 2/2 | 两格在 revision 之后仍有 `attempt_completed`（含 succeeded） |
| 容量软拒绝短语 | 0 | 收据中未见 `Revision requires a remaining experiment slot` / `remaining planning step` / `successful test of the current explanation version` |
| 修订合同拒绝 | 7 | 均为 `revise_hypothesis` 缺 `rationale`（`model_response_rejected`），不是容量门禁 |

容量软拒绝在本轮 live 收据里**没有被触发**。  
原因是模型在仍有复验容量时完成修订，或修订请求先被合同层拦住；不是门禁未绑定。  
试点验证的是工程结局（修订后仍能复验并选出 finalist），不是软拒绝触发次数。

## 4. 探索性配对比较（非结论）

`paired_strategy_comparisons` 在 2/2 任务上齐全。差值与 bootstrap 区间仅作描述；失败仍留在完成率分母。样本过小，不能读成策略排名。

## 5. 账本

本轮结束后：已发送 3685 / 4800 次，已知 token 36,590,967 / 54,000,000，未结算预留 613,812。  
未再扩账本。未出现 `unknown_usage` 停机。

## 6. 未启动的队列

`formal`、`formal-independent-72`、`formal-root`、`formal-iid-72` 未改。  
`formal-generated-root`（36 单元）仍然没有研究库，保持未启动。

## 7. 边界

- 6 单元不足以估计软拒绝触发率。
- 科学结果（0 supported）不解释为策略失败或成功。
- 下一步若要扩大，须另开队列与账本计划；本文件不开 live、不启动 `formal-generated-root`。
