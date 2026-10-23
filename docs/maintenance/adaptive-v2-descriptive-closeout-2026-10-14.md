# Adaptive v2 描述性方法学收口（2026-10-14）

声明：本文只收束已锁描述性证据与门禁语义，不宣称三种策略优劣，不合并各队列 supported 做排名，不开 `--live`，不重开已锁目录，不启动 `formal-generated-root`。  
数字只摘自 [`adaptive-v2-cohort-index-2026-10-14.md`](adaptive-v2-cohort-index-2026-10-14.md) 及各队列已锁 synthesis；不重新出具旧报告。  
比较单位是 **task**；重复不是独立数据集。失败留在分母。

---

## 1. 宣称分类表（可写 / 不可写）

| 队列 | 可写 | 不可写 |
|---|---|---|
| **formal**（第一轮，公开+生成 72） | 流水线可完成（确认 66/72）；公开分区无 supported/refuted、几乎全 inconclusive；6 supported / 21 refuted 全在生成区；工程分母与科学反驳分账；探索性配对仅作描述 | 策略排名；公开行级划分上的机制/新发现；把 supported 当策略优势；跨队列合并排名 |
| **formal-independent-72**（N01–N12，72） | 全新生成任务、与 formal 分账；确认 61/72；supported 0、refuted 30、inconclusive 31、not_evaluated 11；失败留在分母 | 与 formal 合并排名；把 supported 从 6→0 读成策略失效；策略优劣 |
| **formal-root**（U01–U06 + R01–R06，72） | 公开/生成分区分账；确认 61/72；公开无 supported/refuted（独立性未验证 → 科学停在证据不足）；4 supported 全在生成区 | 公开分区策略优劣；跨分区合并配对当结论；与前轮合并排名；策略恢复证明 |
| **formal-iid-72**（I01–I12，`iid_row`，72） | 抽样审计指纹下确认 67/72；supported 18、refuted 26、inconclusive 23、not_evaluated 5；5 未评=修订后未复验（工程分母）；探索性配对数字可摘录但标非结论 | 策略排名；把 supported 18 读成相对前轮的恢复/优势；跨队列合成排名；把配对 CI 当优势证明 |
| **pilot-revision-capacity**（PR01–PR02，6） | 工程结局：6/6 确认完成且均有 finalist；软拒绝收据 0；未复现「修订后无复验→未评」；n 不足以估软拒绝触发率 | 策略优劣/恢复证明；用科学计数（0 supported）解释策略成败；外推软拒绝触发率；与 formal 主对照通约 |

门禁烟测（如 `pilot-reject-audit`）不是对照主表，见 [`adaptive-root-cause-governance-2026-10-11.md`](adaptive-root-cause-governance-2026-10-11.md)。

---

## 2. Methods（描述性方法学，不改代码）

门禁演进（时间线，非策略排名）：

```text
契约拒绝可审计 / 预算语义
        →
抽样可审计（iid_row / documented_cluster；废止 independence_verified 布尔授 valid）
        →
修订须留复验容量（实验槽、规划步、当前版本已成功）
```

### 2.1 契约拒绝可审计

每次物理尝试的 `model_response_rejected` 回执带 `attempt_index`、`consumed_send`、`correction_attempt`、校验 `details`；运行时 `response` / `decision_completed` 拒绝记录带 `audit`。便于区分合同层拒绝与科学结局，不再用软跳过堆叠治标。

### 2.2 预算语义（不抬 `scope_calls`）

单元 scope 的发送上限只计逻辑请求的首次发送（`attempt_index == 1`）；契约纠错的第二次发送仍计入全局与 token，不再吞噬单元科学步配额。**不**提高 `scope_calls=24`。

### 2.3 抽样审计

比较质量不再由 `independence_verified` 布尔授予 `valid`（语义见 `prediction_comparison` / 判断路径；旧报告不重开）。仅在声明与数据对得上且单位不少于 30 时可为 `valid`：

- **`iid_row`**：生成器版本 `adaptive-tasks/v1`，每个分组键等于该行特征哈希。
- **`documented_cluster`**：分组键属于预先登记簇，并附可引用抽样记录；无记录则保持 `insufficient_independent_units`。

现有公开 UCI 行无此类记录时，科学判断停在证据不足；描述性预测效果可保留，不得据行级划分声称机制或新发现。详见 [`adaptive-sampling-audit-2026-10-13.md`](adaptive-sampling-audit-2026-10-13.md)。

### 2.4 修订容量（三条软拒绝）

科学语义：新版本不继承旧支持。`validate` 对修订软拒绝当：（1）实验槽已满；（2）已是最后规划步；（3）当前版本尚无成功实验。门禁检查项 `revision_capacity`；试点见下节 Results。不提高单元发送上限。

### 2.5 账本 / 指纹分账

各队列源码指纹与共享账本余量不同，任务身份与确认名单按队列锁定。已锁目录与报告不可变；扩样须**新命名队列**、单独账本计划与预注册，不得重开旧暴露行或旧确认名单。

（可引用 `adaptive_runtime` 发送/纠错与账本记账语义作说明；本文不改 `popper/` 源码。）

---

## 3. Results（摘自索引与已锁 synthesis）

### 3.1 分队列表（跨队列索引）

| 队列 | 规模 | 确认完成 | supported | refuted | inconclusive | not_evaluated | 工程要点 |
|---|---|---|---|---|---|---|---|
| formal | 72 | 66/72 | 6 | 21 | 39 | 6 | 基线；公开无 supported/refuted |
| formal-independent-72 | 72 | 61/72 | 0 | 30 | 31 | 11 | 全新生成；与 formal 分账 |
| formal-root | 72 | 61/72 | 4 | 14 | 43 | 11 | 公开独立性未验证；4 supported 全在生成区 |
| formal-iid-72 | 72 | 67/72 | 18 | 26 | 23 | 5 | 抽样审计；5 未评=修订后未复验 |
| pilot-revision-capacity | 6 | 6/6 | 0 | 1 | 5 | 0 | 修订容量试点；均有 finalist |

来源：[`adaptive-v2-cohort-index-2026-10-14.md`](adaptive-v2-cohort-index-2026-10-14.md)。**不**对上表做策略排名或跨队列合并。

### 3.2 formal-iid-72 探索性配对（非结论）

摘自 [`adaptive-formal-iid-72-synthesis-2026-10-13.md`](adaptive-formal-iid-72-synthesis-2026-10-13.md)：

| 对比 | 齐全任务 | 均值差 | 探索性 bootstrap 95% CI |
|---|---|---|---|
| evidence_driven − generic_iteration | 7/12 | 0.092 | [−0.362, 0.631] |
| evidence_driven − fixed_queue | 12/12 | −0.093 | [−0.217, −0.006] |
| generic_iteration − fixed_queue | 7/12 | −0.202 | [−0.635, 0.054] |

标为**非结论**：失败仍留在完成率分母；数字不是策略排名。

### 3.3 pilot-revision-capacity（仅工程结局）

摘自 [`adaptive-pilot-revision-capacity-synthesis-2026-10-13.md`](adaptive-pilot-revision-capacity-synthesis-2026-10-13.md)：

- 6/6 确认完成且均有 finalist；未复现 iid-72「修订后无复验 → 未评」。
- 容量软拒绝短语在收据中出现 **0** 次（修订发生在仍有容量时，或先被合同层拒绝）。
- **n=6 不足以估计**软拒绝触发率；科学计数（0 supported）不解释为策略成败。

---

## 4. Limitations / 结论边界

1. **公开无 valid**：公开分区因抽样声明无法授予 `valid`，科学判断停在证据不足；不是「再跑一轮就会出现 supported」。
2. **无策略优势**：本文及各已锁 synthesis 均不宣称三种策略优劣；supported 计数与配对 CI 不得读成排名或恢复证明。
3. **软拒绝触发率未观测**：修订容量软拒绝在 pilot 收据中未触发；触发率未知。
4. **队列不可通约**：任务、指纹、账本不同，不得合并 supported 或合成跨队列策略排名。
5. **`formal-generated-root` 未启动且非下一轮**：36 单元保持未启动；扩样须新命名队列，不是重开该绑定。

---

## 5. 收口自检清单

- [x] 宣称分类表已按五队列列出可写 / 不可写
- [x] Methods 覆盖：契约可审计 → 预算语义（不抬 scope_calls）→ 抽样审计 → 修订容量三条软拒绝 → 账本/指纹分账
- [x] Results 数字仅摘自 cohort-index 与已锁 synthesis；iid-72 配对标非结论；pilot 只写工程结局
- [x] Limitations 写明：公开无 valid、无策略优势、软拒绝率未观测、队列不可通约、formal-generated-root 未启动且非下一轮
- [x] 未改 `popper/`、未改 `gates`/`campaign`、未开 live、未启动 formal-generated-root、未重开已锁队列、未 commit/push
- [x] 指针已链：`adaptive-v2-implementation.md`、`adaptive-root-cause-governance-2026-10-11.md`、（可选）cohort-index

**描述性终点达成。Adaptive live 线保持暂停**（不再默认开 `--live`）。  
下一步冻结（默认停止；仅三类条件可重开 live）：[`adaptive-v2-next-frozen-2026-10-14.md`](adaptive-v2-next-frozen-2026-10-14.md)。  
成稿骨架（零 live 扩写提纲）：[`adaptive-v2-manuscript-outline-2026-10-14.md`](adaptive-v2-manuscript-outline-2026-10-14.md)。