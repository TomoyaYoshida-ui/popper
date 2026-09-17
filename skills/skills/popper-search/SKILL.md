---
name: popper-search
description: 文献检索。六源（arXiv/DBLP/OpenAlex/OpenReview/S2/Crossref）检索 + 去重 + 原始回执落库，产出论文库（每条含 source_api + raw_response_id）。当用户要"查文献/建领域论文库/验证某篇论文存在"时使用；是 popper-domain-evidence 的前置。
license: MIT（整合自 ResearchStudio paper_search、scientific-agent-skills paper-lookup）
metadata:
  version: "1.0"
  skill-author: Popper
  sources: ResearchStudio-Idea (paper_search), scientific-agent-skills (paper-lookup)
---

# popper-search — 文献检索

## 什么时候用

- 从种子论文/关键词出发，构建某方向的论文库；
- 单条查证："这篇论文存在吗、元数据对吗"（转 `popper-evidence` 做核验也可）。

## 铁律

1. **无原始回执不入库**：每条论文记录必须含 `source_api + raw_response_id`（API 原始响应的 id）；手工录入标 `manual` 且默认 `unverified`；
2. **去重确定性**：DOI 精确 > 标题归一化 > 年份，三档匹配，不靠 LLM 猜；
3. 检索结果 7 天本地缓存；免费源优先（arXiv/DBLP/OpenAlex/S2/OpenReview/Crossref）。

## 工作流

1. 输入：种子论文 ids / 关键词 / venue+年份范围；
2. 六源并行检索（哪个源可用用哪个，≥3 源）；
3. 确定性去重 → 合并元数据（冲突字段按"权威源优先：Crossref 权威、OpenAlex 富、S2 补开放获取"）；
4. 逐条落 `source_manifest.json`（`source_api + raw_response_id + doi + url + sha256`）；
5. 产出：论文库 + 检索覆盖声明（覆盖了哪些源、哪些时间段、检索式——此声明以后在 scoop-check 里复用）。

## 确定性脚本

- 去重与元数据合并：确定性脚本（三档匹配）；
- 429/限流：指数退避 + 缓存兜底。

## 边界

- 中文文献：走 nature-ref-verifier 的 CNKI/万方通道（`popper-evidence` 内）；
- 未索引文献（人文学科/预印本早期）：标 `unresolvable`，不阻断流程，如实报告。
