# 独立 72 单元描述性综合（2026-10-12）

数据源：`/home/research-runner/adaptive-v2-validation/campaign/formal-independent-72/report.json`。  
声明：`descriptive first cohort; no general superiority claim`。  
比较单位是 **task**；两次重复不是独立数据集。失败留在分母。  
本文件只描述，不宣称三种策略优劣，不声称新机制或通用优势。

本队列是 N01–N12 全新生成任务（12×3×2），**没有公开任务分区**。  
它与已锁定的第一轮 formal 分开记账。见
`adaptive-formal-descriptive-synthesis-2026-10-11.md`。

## 1. 完成率与科学状态

| 策略 | 确认完成 | supported | planned |
|---|---|---|---|
| fixed_queue | 24/24 | 0/24 | 24 |
| generic_iteration | 19/24 | 0/24 | 24 |
| evidence_driven | 18/24 | 0/24 | 24 |

合计确认完成 61/72。  
科学判断：supported 0，refuted 30，inconclusive 31，not_evaluated 11。

工程终态：

- 61 格：开发完成且确认完成
- 8 格：开发失败 → 确认未评
- 2 格：开发完成但无 finalist → 确认未评
- 1 格：开发完成、确认工程失败

报告分区字段 `public` 的计划数全为 0；`generated` 与上表相同。  
确认证据质量：已完成确认的 61 格为 `valid`，11 格没有确认证据。

## 2. 工程分母（11 格，与科学反驳分开）

| 类别 | 数 | 单元 |
|---|---|---|
| 单元发送预算耗尽，无 finalist | 3 | N07-generic_iteration-r0，N08-evidence_driven-r1，N10-generic_iteration-r0 |
| 鉴权失败，无成功实验 | 1 | N07-evidence_driven-r1 |
| 读超时，无成功实验 | 1 | N10-evidence_driven-r1 |
| 连接超时，无成功实验 | 3 | N11-generic_iteration-r0，N11-generic_iteration-r1，N11-evidence_driven-r0 |
| 决策上限，成功实验被修订取代，无 finalist | 2 | N06-generic_iteration-r1，N06-evidence_driven-r0 |
| 确认实现失败（开发已有 finalist） | 1 | N02-evidence_driven-r1 |

`N02-evidence_driven-r1` 的开发错误是 `request_budget_exhausted`，但已有 finalist，开发状态仍是 `development_completed`；科学 `not_evaluated` 来自确认阶段 `implementation_failed`。  
refuted 30 与 inconclusive 31 是预注册判断，不是上表里的工程失败。

## 3. 探索性配对比较（非结论）

`paired_strategy_comparisons` 只在两侧任务均值都齐全的任务上计算。任务均值齐全 26/36。

| 对比 | 齐全任务 | 均值差 | 探索性 bootstrap 95% CI |
|---|---|---|---|
| evidence_driven − generic_iteration | 6/12 | −0.0015 | [−0.011, 0.009] |
| evidence_driven − fixed_queue | 6/12 | −0.012 | [−0.017, −0.007] |
| generic_iteration − fixed_queue | 8/12 | −0.011 | [−0.018, −0.005] |

后两组区间落在负侧，但齐全任务只有 6/12 与 8/12。失败仍留在完成率分母。  
这些数字不是策略排名。

## 4. 与第一轮 formal 的描述性对照

| 轮次 | 任务 | 确认完成 | supported | refuted | inconclusive | not_evaluated |
|---|---|---|---|---|---|---|
| 第一轮 formal 生成分区 | T07–T12 | 33/36 | 6 | 21 | 6 | 3 |
| 第一轮 formal 公开分区 | T01–T06 | 33/36 | 0 | 0 | 33 | 3 |
| 本轮 formal-independent-72 | N01–N12 | 61/72 | 0 | 30 | 31 | 11 |

第一轮的 supported 只出现在生成分区。本轮 12 个全新生成任务的 supported 为 0。  
两轮的任务、源码指纹和账本都不同，不能合并成一个策略排名，也不能把 supported 从 6 降到 0 读成策略失效证明。

## 5. 账本

共享账本在本轮结束后：已发送 1929 / 3200 次，已知 token 18,870,028 / 36,000,000，未结算预留 490,186。  
全局上限是 2026-10-11 显式提高到 3200 / 3600 万之后的登记值。

## 6. 下一步

`formal-generated-root`（36 单元）保持未启动。  
随后的公开加生成队列是 `formal-root`，描述见
`adaptive-formal-root-synthesis-2026-10-12.md`。两轮不合并成策略排名。
