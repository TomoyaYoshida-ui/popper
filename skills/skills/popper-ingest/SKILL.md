---
name: popper-ingest
description: 稿件摄入。解析已有稿件（PDF/tex/md/docx，含扫描版 OCR），抽取章节结构、全部引用与全部数字，输出结构清单 + 引用清单 + 数字清单，交给 popper-evidence 核验。当用户要导入已有草稿/被拒稿做"引用核验→拒稿预检→定向修改"时使用。
license: MIT / Apache-2.0（整合自 scientific-agent-skills liteparse/markitdown、nature-skills nature-reader）
metadata:
  version: "1.0"
  skill-author: Popper
  sources: scientific-agent-skills (liteparse, markitdown), nature-skills (nature-reader)
---

# popper-ingest — 稿件摄入

## 什么时候用

- 用户给了已有稿件（草稿/被拒稿/要修改的稿），要从"导入 → 核验 → 定向修改"进入；
- 格式：`.tex` `.md` `.docx` `.pdf`（含扫描版）。

## 铁律

1. **解析失败的部分显式标记，不静默跳过**：报告里必须有"未解析块清单"，并提示用户人工粘贴补充；
2. **引用与数字必须保留原文位置**（章节 + 段落/行号），核验时按位置下钻；
3. 摄入不修改原文——只产出结构化清单与报告，修改在 `popper-write`。

## 工作流

1. **解析**（按格式选通道）：
   - `.tex`/`.md`：pandoc → 结构化文本；
   - `.docx`：pandoc/docx 解析；
   - `.pdf` 文本层：liteparse；
   - **扫描版 PDF：liteparse OCR（bbox 定位）**，OCR 结果标 `ocr_confidence`，低置信块入"未解析块清单"。
2. **抽结构**：章节树 → `structure.json`。
3. **抽引用**：正则 + 解析引用上下文 → 每条 `{位置, 引用文本, DOI(如有), bib 条目}` → 交给 `popper-evidence` 的四索引 + 字段级核验。
4. **抽数字**：识别正文中全部数字（含单位/百分比）→ `numbers.json`（此清单只作审计用；**新写内容必须走 claim 通道，此清单不参与写作**）。
5. **产出稿件状态报告**：结构完整度 / 引用核验结果（Real/Potential/Hallucinated 计数）/ 数字-结论一致性初查 / 未解析块清单。

## 确定性脚本

- 引用抽取与位置标注：脚本化（正则 + 定位），LLM 不参与"找引用"；
- 核验调用 `popper-shared/scripts/verify_citations.py`（四索引）与 nature-ref-verifier 的字段级比对流程。

## 边界

- 摄入质量以 A6 验收（docx/tex/md + 1 篇扫描 PDF 全通过）；
- 解析不出的复杂格式（双栏混排、公式混排）如实降级：标记未解析块，不假装解析成功。

## 参考

- `references/parsing_matrix.md` —— 各格式 × 通道的适用矩阵与已知坑
