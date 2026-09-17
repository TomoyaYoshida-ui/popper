# Popper 统一证据表示 — claim registry

> 全流程唯一证据记账法（整合自 scientific-agent-skills 的 `scientific-writing`，MIT，按 CS 语义改造）。
> **铁律：所有 skill 之间传证据只用这一套表示，不用第二套。**

## 四类 ID

| ID 类 | 含义 | 存放 |
|---|---|---|
| `E` | 来源（论文/API 回执/代码运行/文件） | `source_manifest.json` |
| `C` | 声明（稿件里的一句话级事实/数字声明） | `claims.csv`（存 claim 文本的 hash） |
| `N / M / O / R` | 数字 / 方法 / 结果 / 结论 | `consistency_manifest.json` |

## 文件格式

**source_manifest.json**（每条来源必须含原始回执，否则拒绝入库）：

```json
[{
  "id": "E001",
  "type": "paper | api_response | code_run | file",
  "title": "…",
  "source_api": "openalex | crossref | s2 | arxiv | dblp | openreview | manual",
  "raw_response_id": "…",   // API 原始回执 id；manual 时为空并标记 unverified
  "doi": "…", "url": "…",
  "sha256": "…"
}]
```

**claims.csv**（写作通道的锚点）：

```
claim_id, claim_hash, evidence_ids, status, verifier, verified_at
C001, <sha256>, "E001,E002", unverified, , 
```

`status`：`unverified`（默认，不得进最终稿）→ `verified`（人打开过源确认支撑）→ `disputed`（有矛盾，需裁决）。

**consistency_manifest.json**（数字/方法/结果/结论的一致性底账）：

```json
{
  "numeric": [{"id":"N001","name":"ours_f1","value":0.832,"mean":0.831,"std":0.004,"n_seeds":5,"per_seed":[…],"unit":"","source_run":"run_<id>"}],
  "methods":  [{"id":"M001","name":"lr","value":"3e-4","source_config":"…"}],
  "outcomes": [{"id":"O001","claim_id":"C001","direction":"improves","metric":"ours_f1","baseline":"bm25_f1"}],
  "results":  [{"id":"R001","claim_id":"C004","supported":true,"evidence":"N001"}]
}
```

## 写作通道（SLO-1 = 100% 的构造保证）

- 正文数字唯一入口：`[claim:C001][evidence:E001,E002]`——**裸数字被写作门禁拒绝**（要求改写为 claim 引用或删除）；
- 引用入口：`[[ref:xxx]]`，`xxx` 必须能下钻到 `source_manifest.json` 的原始回执；
- 指标名必须 ∈ 版本化 `metrics.json` 注册表（未知指标名 = L0 失败）。

## 配套确定性脚本（契约见 `../scripts/README.md`）

`audit_claims.py`（claim↔evidence 对齐）/ `check_consistency.py`（跨章节数值冲突）/ `check_references.py` / `verify_citations.py`（四索引）/ `validate_results.py`（results.json vs metrics.json）/ `lint_manuscript.py`（裸数字/占位符拦截）。

## 与来源库的关系

- 借鉴 `scientific-writing` 的 C/N/M/O/R 分类与 fail-closed 校验思路（MIT，可整合）；
- **改造**：其原始语义是临床/实验科学（method=protocol，outcome=人群指标）；本 schema 已按 CS 语义改造（method=代码/超参，result=benchmark 数字，见 `consistency_manifest` 的 `source_run/source_config` 字段）；
- 不采用 ARS 的 Material Passport、nature 的 proposal-first 契约——一套表示，避免五套互斥。
