# 受控 Scoop 重叠 FPR（切片 C）

日期：2026-10-25  
状态：已实现  
前置：切片 A（可引用面）+ 切片 B（离线引文绑定）  
范围：在**固定 fixture + 注入 search/llm/fetch** 上跑端到端 `ScoopRun`，报告独立字段 `controlled_scoop_overlap_fpr`；**不**填 `real_novelty_fpr`，不宣称审计级查新

## 背景

- A3 `fpr` 测的是 **RejectionReview** 合成报告，不是 Scoop 管线。
- 切片 B 的 quote-corpus 只测 **引文绑定** 子规则。
- `corpus_seed` 是方法学占位脚手架，**不是** Scoop 金标语料。

需要一条与 A3 分离的工程探针：受控负例上，Scoop 是否把「本无实质重叠」误判成 completed + 中等及以上重叠。

## 指标

仅对 `gold.material_overlap == false` 的负例：

\[
\text{controlled\_scoop\_overlap\_fpr}
= \frac{\#\{\text{completed} \land \text{step7.level} \le 3\}}{N_{\text{neg}}}
\]

- `level`：1=Full … 5=No Overlap；**≤3 = Medium 及以上重叠**（误报「有实质撞车」）。
- 正例（`material_overlap=true`）只做 sanity，不进分母。
- 另报但不进 FPR：`provisional` 率、abstract_only 率、step5 绑定失败率。

## 最小交付（两阶段写在同一切片，实现可按序提交）

### C1 — Oracle plumbing（先做）

- fixture：≥2 负例 + ≥1 正例；`llm_profile=oracle`（全轴 differ、bound passage）
- 期望：负例 FPR = 0；正例 completed 且 level 偏高重叠（文档写明期望区间）
- 证明 harness / CLI / 报告字段可绿，**不**对外说「查新可靠」

### C2 — Adversarial 区分度（紧随）

- 同一负例换 `llm_profile=adversarial_partial`（系统性高估重叠）
- 得到第一个 **honest** 的非零/可区分 `controlled_scoop_overlap_fpr`（fixture LLM，非生产模型）

## 实现要点

| 产物 | 说明 |
|------|------|
| `tests/fixtures/scoop-controlled/` | `manifest.json` + 每案 `gold.json` / `papers.json` / `candidate.json` / `fulltext/` / 可选 `llm_plan.json` |
| `popper/scoop_measure.py` | 加载 manifest；组装 temp idea/scoop；注入 search/llm/fetch；聚合报告 |
| CLI | `popper experiment evaluate scoop-fpr <case_dir>` → `scoop_fpr_report.json`（**不**写 `a3_report.json`） |
| 测试 | `tests/test_scoop_measure.py` 锁死 C1；C2 至少一案 |
| 文档 | capability-boundary：可报告受控 fixture FPR；仍非审计级；与 A3 fpr 分离 |

## 非目标

- 真实 LLM / `paper_search` / 拉 PDF / vendor `scoop-run`
- 填 `real_novelty_fpr`；把 A3 pass 当 Scoop 验收
- 签名 acceptance、法律/专利级「无新颖性」结论
- 用 `corpus_seed` 冒充金标；把 quote 幻觉率改标为 Scoop FPR
- 第一切片不做 TPR/多源检索/生产模型校准

## 验收

- C1：oracle 负例 FPR=0；报告含 `measurement_kind: controlled_oracle_fixture`（或等价）与 `controlled_scoop_overlap_fpr`
- C2：adversarial 路径可复现；文档标明 fixture LLM
- `real_novelty_fpr` 在任何 A3 报告中仍为 null
- 能力边界不升级为「审计级查新产品」

## 过度宣称红线

Oracle FPR≈0 ≠ 生产查新可靠；A3 `fpr` ≠ Scoop；引文绑定率 ≠ overlap FPR。
