# Scoop 可引用面收紧（切片 A）

日期：2026-10-25  
状态：已实现  
仓库：`research-agent-v2` / 分支 `autopilot`  
范围：硬化规划上下文里的可引用 scoop 命中；**不**宣称审计级 FPR 产品

## 背景

今日 `load_scoop_hits` 在 `step7.status == completed` 时，把 **`step2.json` 的全部检索论文** 投成可引用 `SCOOP-*`。  
这与 Scoop 管线叙事（step4 选中 → step5 全文 + 引文核验 → step7 completed）不一致：未深潜的检索命中也能被规划动作引用。

产品边界（`docs/capability-boundary.md`）仍明确：这不是受控语料 FPR / 反证幻觉率验收产品。本切片只修信任面过宽，不改口径。

## 目标

可引用 scoop 命中 =：

1. 所在 run 的 `step7.json` 为 `status=completed`（既有规则，保留）
2. 论文 id ∈ `step4.json.candidate_paper_ids`
3. 该 id 在 `step5.json.papers` 中存在且 `access == "fulltext"`
4. metadata 仍从 `step2.json` 取（title / source / DOI·arXiv / 摘要摘录）

命中记录可带 `verified_fulltext: true`（可选字段，便于前端/调试；规划校验仍只认 `id`）。

## 非目标

- 不接 `evaluate a3` / `novelty_measure` 作为 Scoop CI gate
- 不做受控标注语料 FPR、签名 acceptance report
- 不禁止 `LIT-*` 单独满足 novelty 类 prompt（既有规则保留）
- 不改 step5 引文核验算法、不改 campaign `scoop_node` 完成语义
- 不宣称「审计级查新」

## 实现要点

### `popper/research/scoop_context.py`

- 抽出 `_verified_fulltext_paper_ids(scoop_dir) -> set[str]`：
  - 读 `step4.json` → `candidate_paper_ids`（须为 list）
  - 读 `step5.json` → `papers` 中 `access == "fulltext"` 的 `paper_id`
  - 返回二者交集；任一文件缺失/损坏 → 空集（该 run 不贡献命中）
- `load_scoop_hits`：在 completed 前提下，只对 step2 中 **`paper_id` 落在上述集合** 的论文调用 `project_scoop_hit`
  - 匹配键固定为管线字段 `paper_id`（见 `scoop.py` `_paper_id` / step4·5）；step2 缺 `paper_id` 时用 `_paper_id(paper)` 重算（与 ScoopRun 一致），不得用 title/DOI 字符串冒充别的键
- 模块 docstring 写明：citable ⊂ completed ∩ selected ∩ fulltext；not audit-grade FPR

### 文档

- `docs/capability-boundary.md` 查新节补一句：可引用 scoop 命中仅来自 completed 且 fulltext 深潜子集（仍非 FPR 产品）

### 测试（`tests/test_scoop_context.py`）

| 用例 | 期望 |
|------|------|
| `test_scoop_hits_exclude_unselected_step2_papers` | completed；step2 两篇；step4/5 只深潜一篇 → `load_scoop_hits` 长度 1 |
| `test_abstract_only_selected_not_citable` | 选中但 step5 `abstract_only` → 该 id 不进列表 |
| 既有 provisional / 上限 12 / 引用门禁用例 | 仍绿；必要时 fixture 补 step4/5，避免假 completed 无深潜时静默空列表破坏旧用例 |

辅助：`_write_scoop` 扩展为可写最小 `step4.json` / `step5.json`，默认与「全部 step2 论文均 fulltext 深潜」兼容，减少旧测试改动面。

## 验收

- 上述新测 + 既有 `test_scoop_context` / 相关 runtime scoop 测通过
- 能力边界文案不声称审计级 FPR
- 无网络、不拉 PDF；只读已落盘 JSON

## 后续（本切片外）

- 离线 quote-verifier 接 A3 幻觉子指标（`real_novelty_fpr` 仍为 null）
- 真正的受控语料 Scoop FPR（单独设计）
