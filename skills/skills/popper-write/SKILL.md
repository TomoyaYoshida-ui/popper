---
name: popper-write
description: 论文写作。把证据/结果落成稿件：逐节起草（IMRaD/venue 模板），数字只经 claim 通道插入，引用带锚点，机制声明绑定消融，中文可润色。当用户要"把跑出来的结果写成一篇稿子"或"按模板补全某节"时使用。
license: MIT（整合 scientific-agent-skills scientific-writing、AI-Research-SKILLs ml-paper-writing）+ Apache-2.0（nature-skills nature-writing/polishing）
metadata:
  version: "1.0"
  skill-author: Popper
  sources: scientific-agent-skills (scientific-writing), AI-Research-SKILLs (ml-paper-writing), nature-skills (nature-writing, nature-polishing)
---

# popper-write — 论文写作

## 什么时候用

- 结果齐了（results.json + evidence 登记完毕），要成稿；
- 已有稿件某节改写/润色/格式转换。

## 铁律

1. **裸数字被拒**：任何数字必须写成 `[claim:Cxxx][evidence:Exxx]`；自由文本里的裸数字 = 门禁拒绝（改写或删除）；
2. **引用未 Real 不得落稿**：`[[ref:xxx]]` 的 xxx 必须 Real（或 Potential 已裁决）；
3. **不凭空补事实**：缺什么就留"待补"标记（lint 拦截占位符进最终稿），禁止 LLM 脑补数字/引用/方法细节；
4. **机制声明绑定消融**：正文每个"因为 X 所以 Y"的机制 claim，必须引用一个消融/反事实（`popper-execute` 产出），否则降级措辞为"现象描述（correlational）"。

## 工作流

1. **选模板**：venue（NeurIPS/ACL/ICML…）→ LaTeX 模板（ml-paper-writing 的模板库）；无 venue 用通用 IMRaD；
2. **搭大纲**：只从已登记证据搭（claims.csv 里的 verified claim + results.json 数字），缺证据的段落留在"待补清单"，不写进正文；
3. **逐节起草**：数字走 claim 通道；引用走 `[[ref]]`；方法节每个数字与 run config 对齐（7-mode Mode 6 检测点）；
4. **机制-消融对齐**：正文机制声明 ↔ 消融结果表，逐条绑定；
5. **语言**：英文默认；中文稿可走 nature-polishing 的润色规范（术语/单位/数值精度/声称漂移扫描）；
6. **收尾三跑**：`audit_claims.py` → `check_consistency.py` → `lint_manuscript.py`，全绿才可交 `popper-review`。

## 确定性脚本

- 全部复用 `popper-evidence` 的脚本族；写作门禁 = lint + audit 的退出码。

## 边界

- 写作只"表达"，不"造数"——数字的来源永远在 `popper-execute` 的 results.json；
- AI 署名与披露：按 venue 政策生成披露声明，不替用户决定是否署名。
