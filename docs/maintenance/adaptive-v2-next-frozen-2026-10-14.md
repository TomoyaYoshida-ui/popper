# Adaptive v2 下一步冻结说明（2026-10-14）

承接 [`adaptive-v2-descriptive-closeout-2026-10-14.md`](adaptive-v2-descriptive-closeout-2026-10-14.md)。  
**描述性终点已达成；默认停止。** Adaptive live 线保持暂停：不开 `--live`，不重开已锁队列，不启动 `formal-generated-root`，不宣称三种策略优劣。

---

## 1. 默认状态

| 项 | 状态 |
|---|---|
| 描述性方法学收口 | 已完成（可写/不可写、Methods、Results 摘录、Limitations、自检） |
| 跨队列索引 | [`adaptive-v2-cohort-index-2026-10-14.md`](adaptive-v2-cohort-index-2026-10-14.md) |
| 已锁对照 / 试点 | `formal`、`formal-independent-72`、`formal-root`、`formal-iid-72`、`pilot-revision-capacity` |
| `formal-generated-root` | 未启动，且**不是**下一轮 |
| live / campaign | **暂停**；无默认下一动作 |

无新触发条件时：只维护文档指针与论文成稿引用，不动运行入口。

---

## 2. 仅三类条件可重开 live

下列任一成立时，才允许讨论或执行付费 `--live`。每一类只做**最小动作**；仍不重开已锁目录，扩样必须**新命名队列**、单独账本与预注册。

### 2.1 软拒绝率：审稿强制要求可观测触发率

- **触发**：外部审稿/方法学审阅明确要求给出修订容量软拒绝的触发率或可观测证据（当前 pilot 收据中为 0，n=6 不足以估计）。
- **最小动作**：预注册**新命名**小规模修订容量专项（新任务身份、新账本计划）；只报工程触发次数与分母，不抬 `scope_calls`，不把科学计数读成策略成败；跑完即锁，回到暂停。
- **不做**：重开 `pilot-revision-capacity`；用 formal 主对照估触发率；为「凑触发」改门禁语义。

### 2.2 宣称抬到可辩护比较

- **触发**：论文或对外宣称要从「描述性流水线/分账」抬到**可辩护的策略比较**（需独立于已锁队列的预注册设计）。
- **最小动作**：先写预注册（假设、分母、失败留分母、比较单位=task、抽样审计声明、停止规则）；再开**新命名**队列与账本；确认名单在全部开发终态后冻结。比较结论只绑定该新队列。
- **不做**：合并已锁队列 supported；把 iid-72 配对 CI 或各队列 supported 差读成优势；重开旧暴露行或旧确认名单。

### 2.3 门禁 / 文档 / 代码矛盾

- **触发**：`gates` 回执、维护文档与 `popper/` 实现就同一检查项（如 `sampling_audit`、`revision_capacity`、预算语义）出现不可调和矛盾，且不跑 live 无法证实或修复验证。
- **最小动作**：先用离线/门禁回归定位；仅当矛盾涉及付费路径语义时，开**最小**付费烟测（新命名或既有烟测槽位策略以治理文档为准），验证后更新文档与指纹说明；不扩成对照主表。
- **不做**：借「修矛盾」重跑 formal 主对照；静默改已锁 synthesis 数字；未记门禁就改生产行为。

---

## 3. 明确不做清单

- 不开 `--live`（除非 §2 某条成立且已按最小动作预注册）。
- 不启动 `formal-generated-root`（36 单元保持未启动）。
- 不重开已锁队列或已锁确认名单 / 暴露行。
- 不合并跨队列 supported，不做策略排名或「恢复证明」。
- 不把 formal-iid-72 探索性配对 CI 当作优势结论。
- 不抬高 `scope_calls=24` 以治标。
- 不把 `pilot-reject-audit` 等门禁烟测当作对照主表。
- 不为「继续跑」而新开 campaign；无 §2 触发则停止。

---

## 4. 论文成稿默认引用

描述性终点下成稿优先：

1. 成稿骨架（可扩写提纲）：[`adaptive-v2-manuscript-outline-2026-10-14.md`](adaptive-v2-manuscript-outline-2026-10-14.md)
2. 收口全文（可写/不可写与边界）：本目录 `adaptive-v2-descriptive-closeout-2026-10-14.md`
3. 跨队列数字索引：`adaptive-v2-cohort-index-2026-10-14.md`
4. 门禁与时间线：`adaptive-root-cause-governance-2026-10-11.md`
5. 各队列细节：索引表「描述文档」列指向的已锁 synthesis

运行语义与入口说明：`adaptive-v2-implementation.md`（不替代收口宣称边界）。