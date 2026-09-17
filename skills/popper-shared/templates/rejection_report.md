# 拒稿风险报告模板（popper-verify 产出）— 两栏：拒稿侧 + 造假侧

## 第一栏：拒稿侧（R1–R7）

| # | 一句话拒稿理由 | 状态 | 证据 | 纠正方向 |
|---|---|---|---|---|
| R1 | 无 novelty | ✅消解 / ⚠️存疑 / ❌未消解 | provisional novelty + 检索覆盖声明（覆盖源/时间段/检索式）+ Scoop-Check per-axis | 补穷尽检索 / 换选题 |
| R2 | incremental | … | novelty_category + δ 与阈值对比 | 提阈值口径 / 补机制证据 |
| R3 | 无消融/机制不可证 | … | 机制 claim ↔ 消融绑定表 | 补消融/反事实 |
| R4 | overclaim | … | L1 contradiction 判定 + 证据方向 | 降级措辞 |
| R5 | metric gaming | … | 预注册表 + 重采样 + 密封测试集 | 补预注册 / 修代码 |
| R6 | 泛化弱 | … | R6_evidence{setup, metric, delta, threshold} | 补 held-out 分布 |
| R7 | 问题不重要 | … | 前瞻信号清单 + 证据引用 | 补 importance 信号 |

## 第二栏：造假侧（7-mode）

| Mode | 风险 | 状态 | 检测信号 |
|---|---|---|---|
| 1 | 实现 bug 通过自评 | … | exit≠0 / warning / 效应量可疑地整 |
| 2 | 幻觉引用 | … | 四索引 lookup_verified |
| 3 | 幻觉实验结果 | … | "X% 提升" ↔ results.json 字段 |
| 4 | 捷径依赖 | … | 消融对象 ≠ 声称机制 |
| 5 | bug 被写成新发现 | … | "surprisingly/unexpectedly" 无反向文献 |
| 6 | Methods 造假 | … | Methods 数字 ↔ run config |
| 7 | frame-lock | … | 讨论含"in hindsight" |

**判定**：任何一条 ❌/存疑 → 返回明确纠正方向，不通过（必要条件过滤器）；用户可逐条复核/驳回（理由入审计日志）。
