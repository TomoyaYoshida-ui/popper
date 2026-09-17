---
name: popper-verify
description: 拒稿风险预检。投稿前逐条消解 R1–R7（拒稿侧）+ 7-mode（造假侧），输出两栏拒稿风险报告，任何一条无法消解即返回明确纠正方向。当用户要"投稿前把稿子过一遍风险预检"时使用——本套 skills 的最终质检关。
license: CC-BY-NC（借鉴 ARS 7-mode，重写实现）+ 自研 R1–R7 分类树
metadata:
  version: "1.0"
  skill-author: Popper
  sources: academic-research-skills (ai_research_failure_modes，仅借鉴), 自研 (R1–R7 分类树)
---

# popper-verify — 拒稿风险预检

## 什么时候用

- 定稿前最后一道关（模式 A 步骤 9 之后、10 之前）；
- 模式 B 按需：对单段文字/单个 claim 触发轻量预检（R4 方向一致性 + R1 查新为主）。

## 铁律

1. **必要条件过滤器**：R1–R7 + 7-mode 任何一条无法消解 → 不通过，返回明确纠正方向；"通过"≠"会被录用"；
2. **机器与人的分工**：R1–R5 可机械/半机械消解；R6/R7 机器产证据、用户裁决充分性；
3. **R1 永不二值**：只输出 provisional novelty + 检索覆盖声明（复用 scoop-check 的证据包）；
4. 用户可逐条复核/驳回，驳回理由入审计日志。

## 工作流

1. 汇总证据：`popper-scoop-check` 的证据包（R1）+ `popper-execute` 的 results.json/消融（R2/R3/R5）+ `popper-evidence` 的 claim/引用底账（R2/R4）+ `popper-design` 的预注册表（R5）；
2. 逐条判定 R1–R7（拒稿侧）：
   - R1 用 scoop-check 证据包；R2 用 novelty_category + δ vs 预注册阈值；R3 用机制-消融绑定表；R4 用 L1 contradiction 判定；R5 用预注册 + 重采样 + 密封通道审计；R6 用 held-out 分布证据；R7 用前瞻信号清单；
3. 逐条判定 7-mode（造假侧）：
   - exit≠0/warning 拦（M1）；四索引 lookup_verified（M2）；"X% 提升"↔results.json 字段（M3）；消融对象≠声称机制（M4）；"surprisingly"无反向文献（M5）；Methods 数字↔run config（M6）；"in hindsight"回溯（M7）；
4. 产出两栏报告（`popper-shared/templates/rejection_report.md`），每条附状态 + 证据 + 纠正方向；
5. 有 ❌ → 返回 `popper-write`/`popper-execute` 修，不进入物化。

## 确定性脚本

- 两栏报告的"状态"列：能脚本化的（引用核验、数字对齐、消融绑定、exit code、预注册完整性）全脚本化，LLM 只做语义判定项（R6/R7/消融对象判断）。

## 边界

- R1–R7 分类树的构造有效性靠语料（PeerRead/OpenReview 归纳 + 双人标注 + α≥0.67 + held-out 切分）——**这是自研资产，语料与标注手册入仓**；
- 本 skill 是"守门员"，不产新内容。
