# 工作树验证记录

对应**当前未提交工作树**，不是 `docs/maintenance/completion.md` 里的历史快照数字。

日期：2026-10-23（本机 Windows）。

## 命令与结果

| 命令 | 结果 |
|------|------|
| 产品主路径相关（campaign/ideation/literature_context/chat 数字门禁/research_runtime/controller/claim_projection） | **104 passed**, 5 subtests passed（约 56s） |
| 再扩 harness / tree_search / sampling_audit | **135 passed**, 16 subtests passed（约 53s） |
| `tests/test_campaign_nodes.py` + `test_approve_with_materialize_realizes_manuscript`（跳过语义修后） | **31 passed**（约 18s） |
| `cd frontend && npm test`（vitest run） | **25 files / 169 tests passed** |
| 裸全量 `python -m pytest -q --tb=line` | 收集失败：缺 `httpx2`（adaptive 三文件）。预存环境依赖，未为绿改代码。 |
| 全量并 `--ignore` 上述三个 adaptive 文件 | **1731 passed**, 30 skipped, **3 failed**, 1 teardown error（约 15m41s）。见下「已修」。 |

### 本轮修复后的复验（全量里那 3 fail + 1 error）

| 命令 | 结果 |
|------|------|
| `test_external_mode` 外部确认 + oscillator prediction 对象 + `test_chat_numeric_gate` | **6 passed**（约 25s） |
| oscillator 全文件 + external_mode + external_confirmation + confirmation_bundle + numeric_gate | **50 passed**, 1 skipped（约 57s） |
| `test_campaign_approval_http` + oscillator + external_mode | **43 passed**（约 52s） |
| `test_campaign_nodes` + numeric_gate + claim_projection + research_controller | **79 passed**（约 28s） |

### 根因与改动（摘要）

1. **external_mode `test_exposure.consumed=False`**  
   开发评分后候选刻意保持 `inconclusive`（科学支持留给确认判断），但确认包导出仍按 `supported_in_scope` 筛候选 → `prepare_external_confirmation` 失败，`consumed` 从未置真。  
   **修**：`confirmation_bundle.export_confirmation_bundle` 与边界路径一致，按决策的非对照 `dev` observation_refs 唯一绑定候选。

2. **oscillator `proposal must be one complete JSON object`**  
   数字结论门禁把提案 JSON 里的 `[0.01, 0.04]` 当成区间散文，整段替换为拒绝文案。  
   **修**：`gate_numeric_claims` 对完整 JSON（含 ` ```json ` 围栏）原样放行；补门禁单测。

3. **Windows teardown `campaign.sqlite` 占用**  
   **修**：oscillator 夹具 `TemporaryDirectory(ignore_cleanup_errors=True)`。

### 仍未解决 / 未改环境

- adaptive 三文件收集需 `httpx2`：`popper.capabilities` 的 `agent` extra 探测含 `httpx2`，但 `pyproject.toml` `[project.optional-dependencies].agent` 未声明该包；本机未安装。按约定不为此大改环境；全量仍可 `--ignore` 那三个文件。

## 本轮契约与跳过语义（摘要）

- 默认产品路径：已登记研究 → ResearchController + ResearchRuntime。
- `popper adaptive`：描述性基准，默认不开 live。
- `research campaign`：编排图；未真正执行 → `outcome=skipped`（含 dispatch / propose / variant / 缺配置的 materialize）；已有产物幂等复用仍 `success`。
