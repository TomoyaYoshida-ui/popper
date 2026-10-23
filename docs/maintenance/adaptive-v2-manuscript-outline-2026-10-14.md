# Adaptive v2 成稿骨架（2026-10-14）

声明：本稿骨架仅供描述性方法学成稿扩写，**不开 `--live`**，不重开已锁队列，不启动 `formal-generated-root`，不宣称三种策略优劣。  
数字只摘自 [`adaptive-v2-cohort-index-2026-10-14.md`](adaptive-v2-cohort-index-2026-10-14.md)、[`adaptive-v2-descriptive-closeout-2026-10-14.md`](adaptive-v2-descriptive-closeout-2026-10-14.md) 及各队列已锁 synthesis；不发明数字、不重新出具旧报告。  
比较单位是 **task**；重复不是独立数据集。失败留在分母。

主入口：[`adaptive-v2-descriptive-closeout-2026-10-14.md`](adaptive-v2-descriptive-closeout-2026-10-14.md)。  
下一步冻结：[`adaptive-v2-next-frozen-2026-10-14.md`](adaptive-v2-next-frozen-2026-10-14.md)。  
正文草稿：[`adaptive-v2-manuscript-draft-2026-10-14.md`](adaptive-v2-manuscript-draft-2026-10-14.md)。

---

## 1. 标题候选 + 一句话贡献

**标题候选（择一或微调）：**

1. 可审计门禁下的自适应科学代理：分账描述性评估协议  
2. From Soft Skips to Auditable Gates: A Descriptive Protocol for Adaptive Scientific Agents  
3. 契约可审计、抽样可声明、修订留容量：自适应实验流水线的描述性方法学收口  

**一句话贡献：**  
提出并实证记录一套**协议/门禁可审计的描述性方法**——将合同层拒绝、预算语义、抽样声明与修订复验容量分账，使流水线结局可复述、可门禁；**不是**三种策略的排名或优势证明。

---

## 2. Abstract 要点（子弹）

1. **问题**：自适应科学代理流水线中，软跳过与布尔「独立性」易掩盖合同失败、预算吞噬与不可辩护的 valid，导致科学结局与工程分母纠缠。  
2. **方法轴**：契约拒绝可审计 → 预算语义（不抬 `scope_calls=24`）→ 抽样审计（`iid_row` / `documented_cluster`；废止 `independence_verified` 布尔授 valid）→ 修订须留复验容量（三条软拒绝；检查项 `revision_capacity`）。  
3. **设计**：多队列分账；比较单位=task；失败留分母；已锁目录不可变；扩样须新命名队列。  
4. **结果（工程可完成性）**：五队列确认完成率分别为 formal 66/72、independent 61/72、root 61/72、iid-72 67/72、pilot-revision-capacity 6/6（摘自 cohort-index）。  
5. **结果（科学计数分账，非排名）**：各队列 supported/refuted/inconclusive/not_evaluated 分账报告；公开分区在无抽样记录时无 supported/refuted 或科学停在证据不足。  
6. **探索性配对**：仅 formal-iid-72 报告配对均值差与 bootstrap 95% CI，**明确标为非结论**。  
7. **试点**：修订容量试点 6/6 有 finalist、软拒绝收据 0；n 不足以估触发率。  
8. **边界**：不宣称策略优劣；不合并跨队列 supported；不把配对 CI 当优势；默认不开 live。

---

## 3. Introduction 论证提纲

### 3.1 问题

- 自适应代理在「规划—实验—修订—判断」环中，合同校验失败若被软跳过，科学分母被污染。  
- 单元发送配额被契约纠错吞噬时，抬高 `scope_calls` 属治标。  
- 以布尔 `independence_verified` 授予比较 `valid`，在公开行级划分上易过度宣称机制/新发现。  
- 修订抬高版本后无复验容量 → finalist 空 → `not_evaluated`，工程失败与科学「未支持」易混淆。

### 3.2 治本门禁（贡献定位）

按时间线陈述（非策略排名），映射 closeout §2：

```text
契约拒绝可审计 / 预算语义
        →
抽样可审计（iid_row / documented_cluster）
        →
修订须留复验容量
```

强调：方法学对象是**可审计协议与分账规则**，不是选出「更好策略」。

### 3.3 分账证据（预告 Results）

- 跨队列索引表：完成率与科学四格计数分账。  
- 公开 vs 生成分区：公开无 valid / 证据不足时不得声称机制。  
- iid-72：抽样审计指纹下的描述性计数 + 探索性配对（非结论）。  
- pilot：仅工程结局（复验容量未复现「修订后未评」）。

### 3.4 限制（预告 Discussion）

- 队列任务/指纹/账本不同 → 不可通约排名。  
- 软拒绝触发率未观测。  
- `formal-generated-root` 未启动且非下一轮。  
- 成稿停留在描述性终点（见 next-frozen）。

---

## 4. Methods 节提纲

映射 closeout §2；代码概念名即可，不贴实现补丁。

| 小节 | 内容要点 | 概念名 / 检查项 |
|---|---|---|
| 4.1 流水线与比较单位 | 任务×策略×设定；比较单位=task；失败留分母；重复≠独立数据集 | task、finalist、确认完成 |
| 4.2 契约拒绝可审计 | 每次物理尝试回执：`attempt_index`、`consumed_send`、`correction_attempt`、校验 `details`；区分合同拒绝与科学结局 | `model_response_rejected`、`response` / `decision_completed` 的 `audit` |
| 4.3 预算语义 | 单元 scope 只计逻辑请求首次发送（`attempt_index == 1`）；纠错二次发送计全局/token，**不**抬 `scope_calls=24` | scope 发送上限、`scope_calls` |
| 4.4 抽样审计 | 废止布尔授 valid；`iid_row`（生成器 `adaptive-tasks/v1`，分组键=行特征哈希）；`documented_cluster`（预登记簇+可引用记录）；单位≥30 且声明与数据对得上方可 `valid`；公开 UCI 无记录 → 证据不足 | `sampling_audit`、`prediction_comparison`、`insufficient_independent_units` |
| 4.5 修订容量 | 新版本不继承旧支持；三条软拒绝：实验槽满 / 最后规划步 / 当前版本尚无成功实验 | `revision_capacity`、`validate` |
| 4.6 账本与指纹分账 | 各队列源码指纹与共享账本余量不同；已锁目录不可变；扩样=新命名队列+单独账本+预注册 | 队列指纹、确认名单、暴露行 |
| 4.7 队列设计（描述性） | 简述五队列身份（formal / independent-72 / root / iid-72 / pilot）；门禁烟测（如 `pilot-reject-audit`）**不入**对照主表 | 见 cohort-index |
| 4.8 分析声明 | 科学四格计数；探索性配对仅 iid-72 且非结论；不做跨队列合并排名 | — |

可引用：`adaptive_runtime` 发送/纠错与账本记账语义（说明性）；抽样细节见 `adaptive-sampling-audit-2026-10-13.md`；治理时间线见 `adaptive-root-cause-governance-2026-10-11.md`。

---

## 5. Results 节提纲

### 5.1 跨队列分账总表（主结果）

嵌入或引用索引表关键数字（来源：cohort-index / closeout §3.1）：

| 队列 | 规模 | 确认完成 | supported | refuted | inconclusive | not_evaluated | 工程要点 |
|---|---|---|---|---|---|---|---|
| formal | 72 | 66/72 | 6 | 21 | 39 | 6 | 基线；公开无 supported/refuted |
| formal-independent-72 | 72 | 61/72 | 0 | 30 | 31 | 11 | 全新生成；与 formal 分账 |
| formal-root | 72 | 61/72 | 4 | 14 | 43 | 11 | 公开独立性未验证；4 supported 全在生成区 |
| formal-iid-72 | 72 | 67/72 | 18 | 26 | 23 | 5 | 抽样审计；5 未评=修订后未复验 |
| pilot-revision-capacity | 6 | 6/6 | 0 | 1 | 5 | 0 | 修订容量试点；均有 finalist |

**写法约束：** 只做分账描述；**不**对上表做策略排名或跨队列合并。各队列细节指向已锁 synthesis（见 §8 数据来源）。

### 5.2 分区与宣称边界（叙事段）

映射 closeout 宣称分类表：

- **formal**：公开分区无 supported/refuted、几乎全 inconclusive；6 supported / 21 refuted 全在生成区；工程分母与科学反驳分账。  
- **formal-independent-72**：supported 0；不得与 formal 合并，不得把 6→0 读成策略失效。  
- **formal-root**：公开无 supported/refuted（独立性未验证 → 科学停在证据不足）；4 supported 全在生成区。  
- **formal-iid-72**：5 未评=修订后未复验（工程分母）；supported 18 不得读成相对前轮恢复/优势。  
- **pilot-revision-capacity**：仅工程结局；0 supported 不解释策略成败。

### 5.3 探索性配对（单独小节 · 非结论）

**本节明确标注：探索性、非结论；失败仍留在完成率分母；数字不是策略排名。**

摘自 [`adaptive-formal-iid-72-synthesis-2026-10-13.md`](adaptive-formal-iid-72-synthesis-2026-10-13.md) / closeout §3.2：

| 对比 | 齐全任务 | 均值差 | 探索性 bootstrap 95% CI |
|---|---|---|---|
| evidence_driven − generic_iteration | 7/12 | 0.092 | [−0.362, 0.631] |
| evidence_driven − fixed_queue | 12/12 | −0.093 | [−0.217, −0.006] |
| generic_iteration − fixed_queue | 7/12 | −0.202 | [−0.635, 0.054] |

正文一句模板：「上述 CI 仅描述齐全任务子集上的配对差，**不得**作为策略优势或可辩护比较结论。」

### 5.4 修订容量试点（仅工程结局）

摘自 [`adaptive-pilot-revision-capacity-synthesis-2026-10-13.md`](adaptive-pilot-revision-capacity-synthesis-2026-10-13.md) / closeout §3.3：

- 6/6 确认完成且均有 finalist；未复现 iid-72「修订后无复验 → 未评」。  
- 容量软拒绝短语在收据中出现 **0** 次。  
- **n=6 不足以估计**软拒绝触发率；科学计数（0 supported）不解释为策略成败。

---

## 6. Discussion / Limitations（映射 closeout §4）

1. **公开无 valid**：公开分区因抽样声明无法授予 `valid`，科学判断停在证据不足；不是「再跑一轮就会出现 supported」。  
2. **无策略优势**：不宣称三种策略优劣；supported 计数与配对 CI 不得读成排名或恢复证明。  
3. **软拒绝触发率未观测**：pilot 收据中未触发；触发率未知。  
4. **队列不可通约**：任务、指纹、账本不同，不得合并 supported 或合成跨队列策略排名。  
5. **`formal-generated-root` 未启动且非下一轮**：36 单元保持未启动；扩样须新命名队列，不是重开该绑定。  
6. **（成稿语气）** 描述性终点下，默认维护文档与成稿引用；live 重开仅见 next-frozen 三类条件——**本稿不依赖再开 live 才能成立**。

---

## 7. 本稿不宣称清单

- 三种策略（`evidence_driven` / `generic_iteration` / `fixed_queue`）的优劣或排名。  
- 跨队列合并 supported / 合成「总排名」或「恢复证明」。  
- 把 formal 的 supported 6→independent 的 0 读成策略失效。  
- 把 iid-72 的 supported 18 读成相对前轮的恢复或优势。  
- 把探索性配对 CI（含穿过或未穿过 0 的区间）当作可辩护比较结论。  
- 在公开行级划分上声称机制或新发现（无 `documented_cluster` / 不可授 valid 时）。  
- 用 pilot 科学计数（0 supported）解释策略成败，或外推软拒绝触发率。  
- 将 `pilot-reject-audit` 等门禁烟测当作对照主表。  
- 暗示必须再开 `--live` / 启动 `formal-generated-root` 才能完成描述性成稿。

---

## 8. 图表清单（建议）

| 建议编号 | 类型 | 内容 | 数据来源文件 |
|---|---|---|---|
| Fig 1 | 流程图 | 门禁演进：契约可审计 → 预算语义 → 抽样审计 → 修订容量 | closeout §2；governance `adaptive-root-cause-governance-2026-10-11.md`；sampling-audit `adaptive-sampling-audit-2026-10-13.md` |
| Fig 2 | 示意 | 合同层拒绝 vs 科学四格分账；失败留分母 | closeout §2.1–2.2；implementation `adaptive-v2-implementation.md`（语义说明） |
| Table 1 | 主表 | 五队列：规模、确认完成、supported/refuted/inconclusive/not_evaluated、工程要点 | `adaptive-v2-cohort-index-2026-10-14.md`；closeout §3.1 |
| Table 2 | 宣称边界 | 各队列可写 / 不可写（可缩写为附录） | closeout §1 |
| Table 3 | 探索性（非结论） | iid-72 三组配对：齐全任务、均值差、bootstrap 95% CI | `adaptive-formal-iid-72-synthesis-2026-10-13.md`；closeout §3.2 |
| Table 4 | 工程试点 | pilot-revision-capacity：完成、finalist、软拒绝收据 0 | `adaptive-pilot-revision-capacity-synthesis-2026-10-13.md`；closeout §3.3 |
| Fig/Table A（可选） | 附录 | 分队列补充：formal / independent / root 分区叙事要点 | `adaptive-formal-descriptive-synthesis-2026-10-11.md`、`adaptive-independent-72-synthesis-2026-10-12.md`、`adaptive-formal-root-synthesis-2026-10-12.md` |

制图时数字必须与上表来源逐字一致；不得为「好看」合并队列。

---

## 9. 成稿缺口（仅写作层）

下列为扩写/投稿时的写作层缺口；**均非**「必须再开 live」：

1. **目标期刊/会议格式**：栏目结构、字数、图表上限、补充材料规范未选定。  
2. **参考文献与相关工作**：需补齐自适应实验、LLM 科学代理、可审计评估/门禁协议等领域引用（维护文档未代替文献综述）。  
3. **英文版全文**：本骨架为简体中文提纲；英文 Abstract/全文需另稿翻译与术语统一（`iid_row`、`revision_capacity` 等保留概念名）。  
4. **图稿绘制**：Fig 1–2 仅有文字规范，尚无矢量/排版成图。  
5. **附录材料组织**：各队列 synthesis 长表如何裁剪进补充材料（链接 vs 复述）需按投稿指南决定。  
6. **作者贡献 / 伦理与计算资源声明**：写作模板字段，非实验动作。  
7. **术语表（可选）**：supported / refuted / inconclusive / not_evaluated / finalist / 确认完成 的读者友好定义段。

**明确排除：** 不把「再开 live」「重跑 formal」「启动 formal-generated-root」「估软拒绝触发率」列为成稿必做项。若未来审稿触发 next-frozen §2，属**另案**预注册，不阻塞本描述性骨架扩写。

---

## 10. 扩写顺序建议（可选）

1. Methods（按 §4 填段落）→ Results §5.1–5.2（贴 Table 1 + 宣称边界）  
2. Limitations（§6）与「不宣称」清单嵌入 Discussion  
3. Introduction 收束到贡献句  
4. Abstract 由要点压成段  
5. 探索性配对 §5.3 与试点 §5.4 置后，全程保留「非结论」标签  
6. 图表与英文版按投稿节奏补齐  

默认停止条件见 [`adaptive-v2-next-frozen-2026-10-14.md`](adaptive-v2-next-frozen-2026-10-14.md)。
