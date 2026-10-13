# formal-root 描述性综合（2026-10-12）

数据源：`/home/research-runner/adaptive-v2-validation/campaign/formal-root/report.json`。  
声明：`descriptive first cohort; no general superiority claim`。  
比较单位是 **task**；两次重复不是独立数据集。失败留在分母。  
本文件只描述，不宣称三种策略优劣，不声称新机制或通用优势。

本队列是 U01–U06 六份新 UCI 公开任务，加 R01–R06 六份新生成任务（12×3×2）。  
公开行 `independence_verified` 为 false，不把公开行当成独立分析单位。  
它与已锁定的第一轮 formal、`formal-independent-72` 分开记账。  
`formal-generated-root` 仍未启动。

## 1. 完成率与科学状态

合计确认完成 61/72。  
科学判断：supported 4，refuted 14，inconclusive 43，not_evaluated 11。

| 分区 | 策略 | 确认完成 | supported | refuted | inconclusive | not_evaluated |
|---|---|---|---|---|---|---|
| 公开 U01–U06 | fixed_queue | 12/12 | 0 | 0 | 12 | 0 |
| 公开 U01–U06 | generic_iteration | 11/12 | 0 | 0 | 11 | 1 |
| 公开 U01–U06 | evidence_driven | 12/12 | 0 | 0 | 12 | 0 |
| 生成 R01–R06 | fixed_queue | 12/12 | 2 | 5 | 5 | 0 |
| 生成 R01–R06 | generic_iteration | 9/12 | 0 | 7 | 2 | 3 |
| 生成 R01–R06 | evidence_driven | 5/12 | 2 | 2 | 1 | 7 |

公开分区确认完成 35/36，科学判断全是 inconclusive 或未评，没有 supported、也没有 refuted。  
独立性未验证时，公开任务的科学判断停在证据不足；这不是策略优劣结论。  
4 个 supported 全部在生成分区。

工程终态：

- 61 格：开发完成且确认完成
- 7 格：开发失败 → 确认未评
- 1 格：开发完成但无 finalist → 确认未评
- 3 格：开发完成且有 finalist，确认工程失败

## 2. 工程分母（11 格，与科学反驳分开）

| 类别 | 数 | 单元 |
|---|---|---|
| 单元发送预算耗尽，成功实验被修订取代，无 finalist | 3 | U06-generic_iteration-r1，R02-evidence_driven-r0，R05-generic_iteration-r0 |
| 连接超时，成功实验被修订取代，无 finalist | 1 | R02-generic_iteration-r1 |
| 流中断，无成功实验 | 2 | R03-evidence_driven-r1，R04-evidence_driven-r0 |
| 输出截断，无成功实验 | 1 | R04-evidence_driven-r1 |
| 开发完成，成功实验被修订取代，无 finalist | 1 | R01-evidence_driven-r0 |
| 确认实现失败（开发已有 finalist） | 2 | R01-evidence_driven-r1，R06-generic_iteration-r0 |
| 确认基础设施失败（开发已有 finalist） | 1 | R02-evidence_driven-r1 |

`R02-evidence_driven-r1` 的开发错误是 `request_budget_exhausted`，但已有 finalist，开发状态仍是 `development_completed`；科学 `not_evaluated` 来自确认阶段 `infrastructure_failed`。  
refuted 14 与 inconclusive 43 是预注册判断，不是上表里的工程失败。

## 3. 探索性配对比较（非结论）

只在同一分区内、两侧任务均值都齐全的任务上计算。公开任务 6 个，生成任务 6 个。  
报告文件里的合并配对把两个分区加在一起，这里不采用。

| 分区 | 对比 | 齐全任务 | 均值差 | 探索性 bootstrap 95% CI |
|---|---|---|---|---|
| 公开 | evidence_driven − generic_iteration | 5/6 | 0.118 | [−0.007, 0.243] |
| 公开 | evidence_driven − fixed_queue | 6/6 | 0.206 | [−0.001, 0.433] |
| 公开 | generic_iteration − fixed_queue | 5/6 | 0.035 | [−0.161, 0.232] |
| 生成 | evidence_driven − generic_iteration | 0/6 | — | — |
| 生成 | evidence_driven − fixed_queue | 2/6 | −0.010 | [−0.020, −0.0004] |
| 生成 | generic_iteration − fixed_queue | 3/6 | 0.017 | [−0.004, 0.045] |

公开三组区间都跨过 0。生成分区里 evidence 对 fixed 的区间在负侧，但齐全任务只有 2/6。  
失败仍留在完成率分母。这些数字不是策略排名。

## 4. 与前两轮的描述性对照

| 轮次 | 任务 | 确认完成 | supported | refuted | inconclusive | not_evaluated |
|---|---|---|---|---|---|---|
| 第一轮 formal 公开 | T01–T06 | 33/36 | 0 | 0 | 33 | 3 |
| 第一轮 formal 生成 | T07–T12 | 33/36 | 6 | 21 | 6 | 3 |
| formal-independent-72 | N01–N12 | 61/72 | 0 | 30 | 31 | 11 |
| 本轮公开 | U01–U06 | 35/36 | 0 | 0 | 35 | 1 |
| 本轮生成 | R01–R06 | 26/36 | 4 | 14 | 8 | 10 |

三轮的任务、源码指纹和账本都不同，不能合成一个策略排名。  
本轮生成分区的 supported 为 4，不能读成相对前一轮的策略恢复。

## 5. 账本

开跑前显式把共享上限从 3200 / 36,000,000 提到 4800 / 54,000,000。  
回执是 `ledger-amendment-formal-root.json`。源码指纹未改。

本轮结束后：已发送 2730 / 4800 次，已知 token 27,038,518 / 54,000,000，未结算预留 613,812。

## 6. 未启动的队列

`formal` 与 `formal-independent-72` 未改。  
`formal-generated-root`（36 单元）仍然没有研究库，保持未启动。  
抽样审计指纹已冻；下一已绑定队列是 `formal-iid-72`（live 未开），见
`adaptive-sampling-audit-2026-10-13.md`。
