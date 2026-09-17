---
name: popper-scoop-check
description: 反证查新。对每个候选创新点做 per-axis prior-art 判定 + 对抗式反证 + provisional novelty + 穷尽式覆盖声明，产出可审计的查新证据包。当用户要确认"这个想法有没有人做过"或在 popper-ideate 之后过滤候选时使用。
license: MIT（Scoop-Check 整合自 ResearchStudio-Idea）+ 自研反证（借鉴 ARS，CC-BY-NC 重写）
metadata:
  version: "1.0"
  skill-author: Popper
  sources: ResearchStudio-Idea (scoop_check), academic-research-skills (查新反证思路，仅借鉴)
---

# popper-scoop-check — 反证查新

## 什么时候用

- `popper-ideate` 产出候选后（**必过此步才能进 design**）；
- 用户直接问"这个想法有没有人做过"（模式 B 按需触发）。

## 铁律

1. **永不输出二值"新颖"**：只输出 provisional novelty + 检索覆盖声明；"没找到≠没有"；
2. **反证优先**：先由持反方先验的 agent 构造"该贡献已被覆盖"的最强反驳证据，构造不出来才判 provisional；
3. **反证证据逐条附引用 id**（可下钻原始回执），无引用的反证不算数；
4. 反证幻觉率 > 10%（抽样人工核验）→ 降级"单向检索 + 人工复核"。

## 工作流

1. **per-axis 判定（Scoop-Check）**：输入"研究问题 + 声称 novelty"，对每个轴（问题/方法/数据/结论）输出 overlap 级别 + 最近先验工作清单；
2. **穷尽式覆盖检索**：非 top-k，覆盖 arXiv/DBLP/OpenReview/S2/Crossref，输出覆盖声明（源/时间段/检索式）；
3. **对抗反证**：反方 agent（与正方异族模型/异温）构造最强覆盖证据；反方证据链逐条附引用 ids；
4. **汇总证据包**：
   ```
   候选: IC-001
   逐轴覆盖: [问题轴: 低覆盖 / 方法轴: 高覆盖 → 最近工作 E023, E041]
   provisional novelty: 增量（方法轴被覆盖，问题轴未见）
   覆盖声明: 源=arXiv/DBLP/…, 时间段=2018–2026, 检索式=…
   反证链: 3 条（附引用 id），最强一条: E041 已做 X，未做 Y
   caveat: 覆盖约 X% 相关空间，未覆盖部分不保证
   ```
5. 产出裁决建议（供用户裁决，非机器结论）：通过 / 需修正 / 放弃。

## 确定性脚本

- per-axis 近邻检索与 overlap 计算：`novelty_anchor.py` 复用；
- 覆盖声明的检索式/源/时间段记录：确定性落盘（不可后补改）。

## 边界

- 新颖性的最终裁决归用户 + 审稿人；本 skill 只保证"证据包可审计、覆盖声明诚实"；
- 近 miss 硬样本上 FPR 上升 = 降级信号（A3 验收口径）。
