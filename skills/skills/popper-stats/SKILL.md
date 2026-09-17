---
name: popper-stats
description: 统计审查（支撑 skill）。审查/起草统计报告：实验单位、重复数、p 值、多重比较、效应量、置信区间、图注统计、跨章节数值一致性。当用户要"检查/改写统计小节"或 verify 的 R5 需要统计清单时使用。
license: Apache-2.0 / MIT（整合 nature-skills nature-statistics、scientific-agent-skills statistical-analysis/experimental-design/statistical-power）
metadata:
  version: "1.0"
  skill-author: Popper
  sources: nature-skills (nature-statistics), scientific-agent-skills (statistical-analysis, experimental-design, statistical-power)
---

# popper-stats — 统计审查

## 什么时候用

- 统计小节起草/审查（模式 A 步骤 8 前、`popper-verify` 的 R5 判定依据）；
- 被问"这 p 值报法对不对 / 要不要多重比较校正 / 样本量够不够"。

## 铁律

1. **只审"口径"，不产"数字"**：数值永远来自 results.json，本 skill 检查"怎么报、怎么比"；
2. 主判据 = 预注册表口径（数据重采样 bootstrap/CV），多种子 sanity 只是辅助，不冒充显著性；
3. 搜索跨度多重比较必须纳入 BH-FDR（R5 的统计侧）。

## 审查清单（合并两库）

- **实验单位/重复数**：n 是什么（seed？数据点？），报清楚；
- **p 值与报告规范**：不报裸 p 堆砌；报效应量 + CI，不报"p=0.000"；
- **多重比较**：跨搜索/跨指标校正与否，写明；
- **效应量**：δ 与预注册阈值对比，方向与结论一致；
- **图注统计**：误差线定义（std/SE/CI）、样本量、检验方法；
- **跨章节数值一致性**：同一数字在摘要/正文/表格/图注一致（接 `check_consistency.py`）。

## 确定性脚本

- `stats_check.py`（bootstrap/CV + BH-FDR + paired test，按预注册口径）——统计数字的**计算**全脚本化，LLM 只写解释。

## 边界

- 统计设计（power/样本量）只给"够不够"的提示，不替用户做设计决策。
