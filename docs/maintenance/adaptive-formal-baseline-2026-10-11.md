# Formal 72 单元描述性基线（2026-10-11）

本文件锁死 `/home/research-runner/adaptive-v2-validation/campaign/formal` 本轮结果。
只描述运行与科学分母，不宣称三种策略优劣。不重跑同一 cohort，不覆盖 exposure，
不调用会改写旧全量回执指纹的 `finalize_gates.py`。

`report.json` 声明：`descriptive first cohort; no general superiority claim`。

## 总表

| 策略 | 确认完成 | 支持 | 计划 |
|---|---|---|---|
| fixed_queue | 24/24 | 3/24 | 24 |
| generic_iteration | 21/24 | 1/24 | 24 |
| evidence_driven | 21/24 | 2/24 | 24 |

开发：69 `development_completed`，3 `failed`。  
确认：66 `confirmation_completed`，4 `not_evaluated`，2 `failed`。  
科学：supported 6，refuted 21，inconclusive 39，not_evaluated 6。

## 分区（公开 vs 新生成）

| 分区 | 策略 | 确认完成 | 支持 | 计划 |
|---|---|---|---|---|
| public | fixed_queue | 12/12 | 0/12 | 12 |
| public | generic_iteration | 11/12 | 0/12 | 12 |
| public | evidence_driven | 10/12 | 0/12 | 12 |
| generated | fixed_queue | 12/12 | 3/12 | 12 |
| generated | generic_iteration | 10/12 | 1/12 | 12 |
| generated | evidence_driven | 11/12 | 2/12 | 12 |

公开任务分区上没有任何 `supported`。支持计数不等于策略优势。

## 科学 not_evaluated 分母（6 格）

| 单元 | 开发 | 确认 | 根因类别 | 说明 |
|---|---|---|---|---|
| T04-evidence_driven-r0 | failed | not_evaluated | request_budget_exhausted | 无 finalist；高契约拒绝后撞单元发送上限 |
| T11-generic_iteration-r1 | failed | not_evaluated | request_budget_exhausted | 同上 |
| T08-generic_iteration-r0 | failed | not_evaluated | output_truncated | 无 finalist；规划输出截断且 0 次实验 |
| T01-generic_iteration-r1 | development_completed | not_evaluated | decision_limit | 选择记录 reason=decision_limit，finalist 为空 |
| T06-evidence_driven-r0 | development_completed | failed | confirmation implementation | 有 finalist；确认沙箱失败（原 confirmation-result 仅 ProtocolError） |
| T08-evidence_driven-r0 | development_completed | failed | confirmation implementation | 同上 |

拆分计数：开发预算耗尽 2，输出截断 1，无 finalist（决策上限）1，确认实现失败 2。

## 传输与账本

正式队列：settled 847，unknown 2（读超时 / 流中断），not_sent 1。  
未知用量按 `stop` 协议未重放。全局未结算预留含更早试点记录。  
传输 unknown 不是上述 6 格主因。

## 冻结规则

- 本目录与 `confirmation-roster.json` 为不可变基线。
- 后续工程修复使用新源码指纹与新 cohort 名。
- 根因治理入口见 `adaptive-root-cause-governance-2026-10-11.md`。
- 描述性综合（分区科学状态、探索性配对、账本与下一轮 live 决策）见
  `adaptive-formal-descriptive-synthesis-2026-10-11.md`。
