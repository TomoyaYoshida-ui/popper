---
name: popper-review
description: 对抗性审稿 + rebuttal。投稿前多角色找茬（Journal-Fit + 3 reviewers + Devil's Advocate），0–100 rubric，反谄媚阈值，逐条 rebuttal。当用户要"投稿前模拟审稿"或"写返修回复"时使用。
license: CC-BY-NC（借鉴 ARS reviewer，重写实现）+ MIT（peer-review）+ Apache-2.0（nature-reviewer/nature-response）
metadata:
  version: "1.0"
  skill-author: Popper
  sources: academic-research-skills (academic-paper-reviewer，仅借鉴), scientific-agent-skills (peer-review), nature-skills (nature-reviewer, nature-response)
---

# popper-review — 对抗性审稿 + rebuttal

## 什么时候用

- 稿子写完、投稿前，做一轮"持怀疑态度的 reviewer"预审；
- 收到真实审稿意见，写逐条 rebuttal / 返修信。

## 铁律

1. **默认反方立场**：审稿 agent 持相反先验，逐条找茬，每条意见必须有可见证据（引用/数字下钻），无证据的意见降级为"感觉"；
2. **反谄媚（借鉴 ARS concession 阈值）**：Devil's Advocate 对每轮 rebuttal 打分 1–5，≥4 才可让步；禁止连续让步；
3. **审稿只读**：审稿 agent 不改稿；意见 → 用户裁决 → `popper-write` 改；
4. **rebuttal 逐条闭环**：每条审稿意见必须有"改了什么/为什么不改"的回应，未闭环意见清单必须为 0 才能定稿。

## 工作流

**A. 投稿前预审**（`full` 模式）：
1. Journal-Fit Reviewer（这稿配不配这 venue）；
2. 3 个动态 reviewer（方法/实验/写作三个视角）；
3. Devil's Advocate（最坏意图找茬）；
4. 0–100 rubric 汇总 + 决策映射（≥80 Accept / 65–79 Minor / 50–64 Major / <50 Reject）；
5. 产出评审报告 + 逐条修正建议 → 交 `popper-verify` 复核。

**B. 返修**（`response` 模式）：
1. 解析审稿意见 → 分类（editorial/scientific/statistical/policy）；
2. 每条：改了哪 / 为什么 / 改在哪（标红位置）；
3. 生成逐点回复 + cover letter + 修改说明；
4. rebuttal 自检：每条意见必须闭环。

## 确定性脚本

- 意见-回应闭环核对：脚本比对"意见 id ↔ 回应 id"（未闭环 = 拒绝定稿）；
- rubric 计分：确定性汇总（各 reviewer 分数 + 决策映射表）。

## 边界

- 审稿意见是"找茬参考"，不是录用承诺；用户终审行使否决权；
- 审稿 agent 幻觉（编造"论文里有 X 错误"）→ 用 popper-evidence 逐条复核其证据锚点，幻觉率超阈降级单向审稿。
