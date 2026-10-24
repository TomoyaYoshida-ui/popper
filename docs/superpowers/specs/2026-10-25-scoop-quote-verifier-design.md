# Scoop 离线引文绑定核验（切片 B）

日期：2026-10-25  
状态：待审  
前置：切片 A（`be2210f`，可引用面 = completed ∩ selected ∩ fulltext）  
范围：把 step5 的**字面引文绑定规则**抽成可离线复用的纯函数，并接到 A3 幻觉率子指标的 fixture 路径；**不**宣称审计级 scoop FPR

## 背景

`_verify_fulltext`（`popper/scoop.py`）在 LLM 返回后用硬规则约束 `closest_passage`：

- 去空白规范化后，必须是原文中的**连续**子串
- 长度 20–300 字符

A3（`novelty_measure.py`）的 `measure_hallucination_rate` 在无 `verifier` 时诚实返回 `hallucination_rate: null`；有 verifier 时签名为 `verifier(literature_id) -> bool`，与引文绑定脱节。`real_novelty_fpr` 恒为 `null`。

## 目标

1. 抽出与 LLM 无关的纯函数（建议名 `passage_bound_to_fulltext(passage, content) -> bool`），规则与现网 `_verify_fulltext` 一致；`scoop.py` 改用该函数，行为不变。
2. 提供离线 fixture 适配：给定 `{literature_id: fulltext}` 与声明 `{literature_id, closest_passage}`，用同一规则判定「声称覆盖」是否绑定原文。
3. 扩展幻觉测量路径（最小改动）：当提供 quote-corpus 时，可测 **引文绑定幻觉率**；未提供时仍为 `not_available` / `null`。
4. `real_novelty_fpr` **保持 `null`**；产品文案不写「审计级查新」。

## 非目标

- 不跑真实 ScoopRun / 不联网拉 PDF
- 不把 RejectionReview FPR 改标为 Scoop FPR
- 不改 step6/7 轴对比结论的独立性
- 不改切片 A 的可引用集合

## 实现要点

| 位置 | 改动 |
|------|------|
| 新模块或 `popper/scoop.py` 旁的小模块（如 `popper/quote_binding.py`） | `normalize_passage` + `passage_bound_to_fulltext`；单测锁死边界（19 字失败、恰好 20 字成功、改写失败、跨空白成功） |
| `popper/scoop.py` `_verify_fulltext` | 用共享函数替换内联 `normalize(passage) in normalize(content)` |
| `popper/novelty_measure.py` | 增加 `make_quote_binding_verifier(corpus: dict[str,str])`：对声明查 corpus 文本 + passage，无文本或未绑定 → False；`run(..., quote_corpus=...)` 可选接入；报告字段可增 `quote_binding_hallucination_rate` 或复用既有 `reverse_citation_hallucination_rate`（实现时二选一，文档写清；推荐**复用**既有字段以免双口径） |
| `tests/test_quote_binding.py` + 扩 `test_novelty_measure.py` | 纯函数边界；有/无 corpus 时 A3 报告字段诚实 |
| `docs/capability-boundary.md` | 一句：离线引文绑定可测；仍非受控语料 Scoop FPR |

## 验收

- scoop 全文核验既有测试仍绿（行为不漂）
- 无 corpus → 幻觉率仍为 `null` + `not_available`
- 有 fixture corpus → 可复现数值；故意错引文计入幻觉
- 文档不声称审计级 scoop

## 后续（本切片外）

受控标注语料上的端到端 Scoop FPR（单独设计）。
