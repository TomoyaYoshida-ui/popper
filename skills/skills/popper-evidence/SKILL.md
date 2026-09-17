---
name: popper-evidence
description: 证据系统（全流程地基）。claim registry 记账 + 四索引/字段级引用核验 + 跨章节一致性 lint。任何 skill 要"登记一个数字、核验一条引用、查一个矛盾"都走这里。当用户要"验这条引用真的吗/这个数字从哪来的/这两处数字打架吗"时使用。
license: MIT（整合 scientific-agent-skills scientific-writing）+ 借鉴重写（ARS 四索引 gate，CC-BY-NC）+ Apache-2.0（nature-ref-verifier 字段级校验）
metadata:
  version: "1.0"
  skill-author: Popper
  sources: scientific-agent-skills (scientific-writing), academic-research-skills (四索引 gate，仅借鉴), nature-skills (nature-ref-verifier)
---

# popper-evidence — 证据系统

## 什么时候用

- 写作阶段登记数字/引用（`popper-write` 内部依赖）；
- 模式 B 按需："验这条引用""这个数字下钻到哪""这两处数字是不是打架"。

## 铁律

1. **数字唯一入口**：写作中的数字只能经 `[claim:C001][evidence:E001]` 通道插入；**裸数字被写作门禁拒绝**；
2. **无原始回执的引用不入库**：来源必须含 `source_api + raw_response_id`；
3. **Hallucinated = 门禁失败**；`Potential` = judger 裁决（用户可否决）；`unresolvable`（合法未索引）不 block；
4. 核验是**确定性脚本**的事，LLM 不参与"判断引用真假"。

## 工作流

**A. 登记**（任何 skill 产数字时）：
1. 数字 → `consistency_manifest.json`（N-ID + 来源 run_id）；
2. 声明 → `claims.csv`（C-ID + hash + evidence_ids，默认 `unverified`）；
3. 来源 → `source_manifest.json`（E-ID + 原始回执）。

**B. 引用核验**（两段）：
1. **四索引 gate**（外文）：S2 + OpenAlex + Crossref + arXiv resolver 并行查证 → `lookup_verified{true,false,unresolvable}`；**false 仅限 ID-keyed unmatched**（DOI/arXiv 精确查证失败）；`verification.db` 90 天缓存；
2. **字段级校验**（含中文）：DOI 精确 > 标题归一化 > 年份；逐字段比作者/标题/卷期/页码（Critical/Warning/Info 三级）；检测"DOI 张冠李戴"（DOI 可解析但解析到另一篇）；中文文献走 CNKI/万方通道；
3. 汇总三级：`Real / Potential / Hallucinated`。

**C. 一致性 lint**（写作后、提交前）：
1. `check_consistency.py`：跨章节同义数字是否一致（不一致 = 冲突清单，逐条人工裁决，禁止静默归一）；
2. `audit_claims.py`：claim↔evidence 对齐，未验证 claim 不得进最终稿；
3. `lint_manuscript.py`：裸数字/占位符拦截。

## 确定性脚本

见 `popper-shared/scripts/README.md`：`verify_citations.py` / `audit_claims.py` / `check_consistency.py` / `check_references.py` / `lint_manuscript.py`。全部离线、确定性、可单测。

## 边界

- 本 skill 保证"存在性"（数字有出处）与"一致性"（数字与结论方向相符），**不保证"正确性"**（与外部 ground truth 相符）；
- 正确性只对"外部事实数字"做抽样核验，不承诺 100%。
