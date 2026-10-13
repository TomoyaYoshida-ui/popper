# formal-iid-72 描述性综合（2026-10-13）

数据源：`/home/research-runner/adaptive-v2-validation/campaign/formal-iid-72/report.json`。  
声明：`descriptive first cohort; no general superiority claim`。  
比较单位是 **task**；两次重复不是独立数据集。失败留在分母。  
本文件只描述，不宣称三种策略优劣，不声称新机制或通用优势。

本队列是 I01–I12 十二个 `iid_row` 生成任务（kinds 0–5 各两次，12×3×2），  
在抽样审计指纹下运行。没有公开任务分区。  
它与已锁定的 formal、`formal-independent-72`、`formal-root` 分开记账。  
`formal-generated-root` 仍未启动。

## 1. 完成率与科学状态

| 策略 | 确认完成 | supported | planned |
|---|---|---|---|
| fixed_queue | 24/24 | 8/24 | 24 |
| generic_iteration | 19/24 | 4/24 | 24 |
| evidence_driven | 24/24 | 6/24 | 24 |

合计确认完成 67/72。  
科学判断：supported 18，refuted 26，inconclusive 23，not_evaluated 5。

工程终态：

- 67 格：开发完成且确认完成
- 2 格：开发失败 → 确认未评
- 3 格：开发完成但无 finalist → 确认未评

确认证据质量：已完成确认的 67 格为 `valid`（抽样声明为可审计 `iid_row`），5 格没有确认证据。

## 2. 工程分母（5 格，与科学反驳分开）

| 类别 | 数 | 单元 |
|---|---|---|
| 开发完成，成功实验被修订取代，无 finalist | 3 | I01-generic_iteration-r0，I02-generic_iteration-r1，I03-generic_iteration-r0 |
| 单元发送预算耗尽，成功实验被修订取代，无 finalist | 2 | I10-generic_iteration-r1，I11-generic_iteration-r0 |

refuted 26 与 inconclusive 23 是预注册判断，不是上表里的工程失败。

## 3. 探索性配对比较（非结论）

`paired_strategy_comparisons` 只在两侧任务均值都齐全的任务上计算。

| 对比 | 齐全任务 | 均值差 | 探索性 bootstrap 95% CI |
|---|---|---|---|
| evidence_driven − generic_iteration | 7/12 | 0.092 | [−0.362, 0.631] |
| evidence_driven − fixed_queue | 12/12 | −0.093 | [−0.217, −0.006] |
| generic_iteration − fixed_queue | 7/12 | −0.202 | [−0.635, 0.054] |

evidence 对 fixed 的区间在负侧且 12/12 齐全，但仍是探索性描述；失败留在完成率分母。  
这些数字不是策略排名。

## 4. 与前几轮生成分区的描述性对照

| 轮次 | 任务 | 确认完成 | supported | refuted | inconclusive | not_evaluated |
|---|---|---|---|---|---|---|
| 第一轮 formal 生成 | T07–T12 | 33/36 | 6 | 21 | 6 | 3 |
| formal-independent-72 | N01–N12 | 61/72 | 0 | 30 | 31 | 11 |
| formal-root 生成 | R01–R06 | 26/36 | 4 | 14 | 8 | 10 |
| 本轮 formal-iid-72 | I01–I12 | 67/72 | 18 | 26 | 23 | 5 |

任务、源码指纹和账本都不同，不能合成一个策略排名。  
本轮 supported 为 18，不能读成相对前几轮的策略恢复或优势证明。  
本轮比较质量按抽样审计门禁核对；前几轮报告不重开。

## 5. 账本

开跑前余量已盖住最坏 72×24 次 / 1800 万 token，未再扩账本。  
本轮结束后：已发送 3611 / 4800 次，已知 token 35,804,018 / 54,000,000，未结算预留 613,812。

## 6. 未启动的队列

`formal`、`formal-independent-72`、`formal-root` 未改。  
`formal-generated-root`（36 单元）仍然没有研究库，保持未启动。

## 7. 工程根因（本轮之后）

5 个未评不是「再开一轮生成对照」能修的。  
修订抬高假设版本后，旧成功不可作 finalist；模型若未再成功复验就耗尽决策或发送预算，只能 `not_evaluated`。  
后续源码对修订加复验容量软拒绝（实验槽、规划步、当前版本须已成功）；
该指纹已冻入门禁。不改本轮数字，不宣称策略优势，不开 live。
