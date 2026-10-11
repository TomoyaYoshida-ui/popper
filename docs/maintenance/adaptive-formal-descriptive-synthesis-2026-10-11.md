# Formal 第一轮描述性综合（2026-10-11）

数据源：`/home/research-runner/adaptive-v2-validation/campaign/formal/report.json`。  
声明：`descriptive first cohort; no general superiority claim`。  
比较单位是 **task**；两次重复不是独立数据集。失败留在分母。  
本文件只描述，不宣称三种策略优劣，不声称新机制或通用优势。

## 1. 完成率与科学状态

| 策略 | 确认完成 | supported | planned |
|---|---|---|---|
| fixed_queue | 24/24 | 3/24 | 24 |
| generic_iteration | 21/24 | 1/24 | 24 |
| evidence_driven | 21/24 | 2/24 | 24 |

科学判断合计：supported 6，refuted 21，inconclusive 39，not_evaluated 6。

工程终态：

- 66 格：开发完成且确认完成  
- 3 格：开发失败 → 确认未评  
- 1 格：开发完成但无 finalist → 确认未评  
- 2 格：开发完成、确认工程失败  

## 2. 分区：公开任务 vs 新生成任务

| 分区 | supported | refuted | inconclusive | not_evaluated |
|---|---|---|---|---|
| public | 0 | 0 | 33 | 3 |
| generated | 6 | 21 | 6 | 3 |

公开分区没有任何 `supported` 或 `refuted`，几乎全是 inconclusive。  
全部 6 个 supported 与全部 21 个 refuted 都落在生成任务分区。

确认证据质量字段：`insufficient_independent_units` 33，`valid` 33。  
与实施说明一致：公开数据缺少可靠独立分析单位时，不得据行级划分声称机制或新发现；描述性效果可保留，统计支持不能外推。

## 3. 探索性配对比较（非结论）

`paired_strategy_comparisons` 只在**两侧任务均值都齐全**的任务上计算，失败仍留在完成率分母：

| 对比 | 齐全任务 | 均值差 | 探索性 bootstrap 95% CI |
|---|---|---|---|
| evidence_driven − generic_iteration | 7/12 | +0.085 | [0.001, 0.224] |
| evidence_driven − fixed_queue | 9/12 | −0.067 | [−0.215, 0.022] |
| generic_iteration − fixed_queue | 9/12 | −0.120 | [−0.287, 0.007] |

后两组区间跨 0 或含 0。第一组区间虽落在正侧，但只有 7/12 任务齐全，且单位是探索性 task 聚合。  
**不能**据此宣称证据驱动更优。

## 4. 工程分母（与科学反驳分开）

6 个科学 `not_evaluated`：

| 类别 | 数 | 单元 |
|---|---|---|
| 单元发送预算耗尽、无 finalist | 2 | T04-evidence_driven-r0，T11-generic_iteration-r1 |
| 规划输出截断、无实验 | 1 | T08-generic_iteration-r0 |
| 决策上限且无 finalist | 1 | T01-generic_iteration-r1 |
| 确认沙箱实现失败 | 2 | T06-evidence_driven-r0，T08-evidence_driven-r0 |

传输 unknown（formal 内 2 条）不是这 6 格主因。  
根因治理后的复验见 `adaptive-root-cause-governance-2026-10-11.md`（`pilot-reject-audit` 6/6 确认完成）。

## 5. 可读结论（描述性）

1. 本轮是**可完成的第一轮对照流水线**，不是策略排名实验的终点。  
2. 科学信号几乎全部来自**生成任务**；公开任务因独立性不足，判断停留在 inconclusive。  
3. 工程缺口（预算×契约拒绝、确认实现、截断）已单独记账，并在后续源码中按结构修复；修复**不改写**本轮 formal 数字。  
4. supported 计数不能当作策略优势；配对 CI 也不能当作优势证明。

## 6. 对下一轮 live 的含义

`formal-independent-72` 已绑定根因治理指纹，使用 N01–N12 全新生成任务，不覆盖本轮 formal。

按 `pilot-reject-audit` 粗估：约 48 个模型开发单元 × 均 14 次发送，加盲评，合计约 **850+ 次发送**、约 **720 万+ token**。  
当前共享账本剩余约 **465 次发送**、约 **694 万 token（含未结算预留后的粗算）**。  
**在现有 1600 / 1800 万全局上限下，不足以安全跑完独立 72 单元。**

2026-10-11 晚间已执行扩账本：共享 `model-calls.sqlite` 全局上限由
1600 / 1800 万提高到 **3200 / 3600 万**（见 campaign
`ledger-amendment-independent-72.json`）。`formal-independent-72` 的
`global_call_cap` / `global_token_cap` 同步写入，源码改为从 cohort 读取上限，
并重新绑定指纹后启动 `--live`。仍不宣称策略优势。
