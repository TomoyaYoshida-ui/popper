---
name: popper-ideate
description: 面向计算机大类的候选创新点生成。从领域语料（gap/矛盾/负结果/前瞻信号）出发，用 ideation 算子与 15 类选题 pattern 生成"选题/方法/发现"三类候选贡献，输出锚定证据的创新点卡（Title · Motivation · Method · 先验覆盖）。当用户要"从几篇种子论文找一个 CS 方向的创新点"、要生成候选研究问题或方法提案时使用；生成后交给 popper-scoop-check 反证过滤。
license: MIT（整合自 ResearchStudio-Idea / AI-Research-SKILLs，均 MIT；提案算子自研）
metadata:
  version: "1.0"
  skill-author: Popper
  sources: ResearchStudio-Idea (idea_spark), AI-Research-SKILLs (brainstorming-research-ideas, creative-thinking-for-research)
---

# popper-ideate — 候选创新点生成

## 什么时候用

- 用户给了几篇种子论文 / 一个研究方向，要"找 CS 方向的创新点"；
- 要生成候选贡献：新问题（选题）、新方法、从数据/负结果里挖新规律（发现）；
- 注意：本 skill **只生成候选，不判断新不新**——判断交给 `popper-scoop-check`；生成前**必须先有领域语料**（`popper-domain-evidence` 的产物）。

## 铁律（两条）

1. **候选只能从领域语料的记录派生，不能凭空生成**：每个候选必须能下钻到它的派生依据（哪条 gap/矛盾/负结果/前瞻信号 → 哪篇文献）。违反 = 候选作废。
2. **LLM 只做算子内实例化，不得发明新算子**：算子清单固定（见下），要新增算子须用户显式登记（入审计日志）。

## 输入

- `popper-domain-evidence` 产出的领域语料：`domain_evidence.json`（gap/矛盾/负结果/前瞻信号四类记录，每条锚定文献 id）；
- 可选：用户约束（compute 预算、目标 venue、偏好的创新类型）。

## 工作流

### 1. 选创新类型（三类，对应不同算子）

| 创新类型 | 从哪些记录派生 | 用的算子 |
|---|---|---|
| 选题创新 | gap / 前瞻信号 | 直接派生新问题、新视角 |
| 方法创新 | 矛盾 / gap（方法族卡住处） | replace-module / cross-domain-transfer / counterfactual-mechanism |
| 发现创新 | 负结果 / 数据缺口 | data-deficit-to-task / negative-result-pivot |

### 2. 选 ideation pattern（ML 域直接用 15 类，非 ML 域复用方法学）

15 类 pattern 清单与每类的"何时适用、怎么套"见 `references/ideation_patterns.md`（源自 ResearchStudio-Idea 对 1,947 篇 ICLR/ICML/NeurIPS 录用论文的归纳）。**非 ML CS 子领域（系统/理论/安全/PL/网络/数据库/HCI）不套 ML 权重**，只用"读领域 → 找瓶颈 → 选模式"的方法学，pattern 的"什么算好 idea"用该子领域文献校准。

### 3. 提案算子（自研，显式、领域无关）

- `replace-module`：把已有方法的某模块换成另一机制；
- `cross-domain-transfer`：把 A 子领域的方法搬到 B 问题（用 creative-thinking 的 bisociation/结构映射做细节）；
- `counterfactual-mechanism`：假设一个反事实机制，设计可证伪的检验；
- `data-deficit-to-task`：从"缺某类数据"派生新任务/新数据集；
- `negative-result-pivot`：把负结果/被拒假设反过来当选题。

每个候选写清：**用了哪个算子 + 作用在领域语料的哪条记录上 + 生成的假设是什么**。

### 4. 生成候选创新点卡

每个候选输出一张卡，**字段固定**（见 `popper-shared/templates/idea_card.md`）：

```yaml
id: IC-001
type: 选题 | 方法 | 发现
title: 一句话标题
motivation: 为什么要做（锚定哪条 gap/矛盾/负结果，引用其文献 id）
method: 怎么做（可证伪的最小实验设计）
operator: 用了哪个算子
derived_from: [领域语料的记录 id]
novelty_category: 新问题 | 新数据 | 新方法族 | 新连接 | 增量delta
novelty_hint: 初步分级（重大/增量/平凡，**provisional，最终由 scoop-check + 用户裁决**）
```

### 5. 交给过滤层

生成 N≥3 个候选后，**立即转 `popper-scoop-check`**，不要在没过滤前写作。过滤通过的候选才进入 `popper-design`。

## 确定性脚本

- novelty 分级里的"近邻文献锚定"用 `popper-shared/scripts/novelty_anchor.py`（对 domain_evidence.json 做检索 + 多类别分类，输出近邻文献 id）；
- 候选卡的 schema 校验用 `popper-shared/scripts/validate_idea_card.py`（字段齐全 + derived_from 全命中领域语料记录，否则 L0 拒绝）。

## 边界（诚实声明）

- 本 skill 承诺"候选锚定证据 + defensible"，**不承诺"保证找到重大创新"**；
- novelty 是 provisional 分级，最终"新不新、值不值得做"归用户 + 审稿人；
- 输出的是"候选"，不是"结论"——没有经过 scoop-check 的候选不得进入设计/写作。

## 参考

- `references/ideation_patterns.md` —— 15 类 pattern 详解（源自 ResearchStudio-Idea）
- `references/creative_thinking.md` —— bisociation / 结构映射 / 约束操纵（源自 AI-Research-SKILLs creative-thinking）
- `popper-shared/templates/idea_card.md` —— 创新点卡模板
