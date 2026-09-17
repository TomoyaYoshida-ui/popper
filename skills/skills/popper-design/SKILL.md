---
name: popper-design
description: 研究设计 + 预注册。把通过反证查新的候选创新点落成可执行的研究协议：假设种群、指标注册表、baseline、数据集、统计检验计划、held-out 切分，全部冻结进预注册表。当用户要"把一个 idea 变成能跑的最小实验设计"时使用。
license: MIT（整合自 scientific-agent-skills hypothesis-generation）+ 自研预注册表
metadata:
  version: "1.0"
  skill-author: Popper
  sources: scientific-agent-skills (hypothesis-generation), 自研 (preregistration 模板)
---

# popper-design — 研究设计 + 预注册

## 什么时候用

- scoop-check 通过的候选，要进入"最小实验"设计；
- 已有稿件/想法要补"可复现的设计协议"。

## 铁律

1. **预注册表不完整 = L0 拒绝**（缺 primary_metric / held_out_split / correction 任一项，不得进入 code-generate）；
2. **冻结后改动需用户审批留痕**（版本 +1，理由入审计日志）——这是 R5 的根基；
3. 假设必须可证伪：写清"什么结果算支持、什么结果算证伪"；
4. held-out 切分在研究设计阶段固化，且**默认密封**（代码不可写、只经受控 evaluator 读取）。

## 工作流

1. **假设种群 H**：从创新点卡派生 ≥2 条可证伪假设（主假设 + 可替代解释）；
2. **指标注册表**：每个指标注册规范名（`ours_f1`）+ 定义 → 版本化 `metrics.json`；
3. **baseline 清单**：强基线（当前 SOTA）+ 弱基线（lower bound），写明"beat 谁算数"；
4. **统计计划**：primary metric / 检验（bootstrap/CV 主判据）/ α / BH-FDR 校正 / 效应量阈值 / n_seeds / 复现容差；
5. **数据集 + held-out 切分 + 泄漏注意**（如：基准的公开 test set 即 held-out，训练/验证期间禁触）；
6. **预算**：搜索轮次 K / token / 算力上限；
7. 全部落入 `popper-shared/templates/preregistration.md` 模板，冻结（记录 frozen_at）。

## 确定性脚本

- `metrics.json` schema 校验（规范名合法、可计算）；
- 预注册完整性检查：缺字段 = L0 拒绝。

## 边界

- 纯选题/理论型研究：跳过实验设计，只做"论证结构预注册"（结论边界 + 引用锚点清单），δ 口径不适用；
- 统计口径的"为什么这么选"要写理由，不写默认值。
