# Adaptive v2 跨队列索引（2026-10-14）

声明：本索引只汇总已锁描述性证据，不宣称三种策略优劣，不合并各队列 supported 做排名。  
失败留在各队列分母。比较单位是 task；重复不是独立数据集。  
数字摘自既有 synthesis / governance，不重新出具旧报告。  
描述性方法学收口（可写/不可写、Methods、边界与自检）：`adaptive-v2-descriptive-closeout-2026-10-14.md`。

## 1. 队列一览

| 队列 | 规模 | 确认完成 | supported | refuted | inconclusive | not_evaluated | 工程要点 | 描述文档 |
|---|---|---|---|---|---|---|---|---|
| formal（第一轮） | 公开+生成 12×3×2=72 | 66/72 | 6 | 21 | 39 | 6 | 基线；公开无 supported/refuted | `adaptive-formal-baseline-2026-10-11.md`、`adaptive-formal-descriptive-synthesis-2026-10-11.md` |
| formal-independent-72 | 生成 N01–N12，72 | 61/72 | 0 | 30 | 31 | 11 | 全新生成任务；与 formal 分账 | `adaptive-independent-72-synthesis-2026-10-12.md` |
| formal-root | 公开 U01–U06 + 生成 R01–R06，72 | 61/72 | 4 | 14 | 43 | 11 | 公开独立性未验证 → 科学停在证据不足；4 supported 全在生成区 | `adaptive-formal-root-synthesis-2026-10-12.md` |
| formal-iid-72 | 生成 I01–I12（`iid_row`），72 | 67/72 | 18 | 26 | 23 | 5 | 抽样审计指纹；5 未评=修订后未复验 | `adaptive-formal-iid-72-synthesis-2026-10-13.md` |
| pilot-revision-capacity | PR01–PR02，6 | 6/6 | 0 | 1 | 5 | 0 | 修订容量指纹；均有 finalist；未复现「修订后无复验→未评」 | `adaptive-pilot-revision-capacity-synthesis-2026-10-13.md` |

门禁/烟测（非对照主表）：`pilot-reject-audit` 等见 `adaptive-root-cause-governance-2026-10-11.md`。

## 2. 门禁演进（时间线，非策略排名）

```text
契约拒绝可审计 / 预算语义
        →
抽样可审计（iid_row / documented_cluster；废止 independence_verified 布尔授 valid）
        →
修订须留复验容量（实验槽、规划步、当前版本已成功）
```

- 抽样审计：`adaptive-sampling-audit-2026-10-13.md`（检查项 `sampling_audit`）。
- 修订容量：治理文档 2026-10-13 条；检查项 `revision_capacity`；试点见上表。

各队列源码指纹与账本不同，不能合成一个策略排名。

## 3. 边界

- 已锁目录与报告不可变：`formal`、`formal-independent-72`、`formal-root`、`formal-iid-72`、`pilot-revision-capacity`。
- `formal-generated-root`（36 单元）**未启动**，且**不是**下一轮实验。
- Adaptive live 线在本索引日期起**暂停**：不再默认开 `--live`。
- 若仍要扩样实证，须**新命名队列**、单独账本计划与预注册；不得重开旧暴露行或旧确认名单。
- 描述性收口全文：`adaptive-v2-descriptive-closeout-2026-10-14.md`。
- 下一步冻结（默认停止）：`adaptive-v2-next-frozen-2026-10-14.md`。
- 成稿骨架（零 live）：`adaptive-v2-manuscript-outline-2026-10-14.md`。