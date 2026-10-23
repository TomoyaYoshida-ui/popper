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
| 裸全量 `python -m pytest -q --tb=line` | 此前收集失败：缺 `httpx2`（adaptive 三文件）。现已声明进 `agent`/`adaptive` extra 并本机安装。 |
| 全量并 `--ignore` 上述三个 adaptive 文件 | **1731 passed**, 30 skipped, **3 failed**, 1 teardown error（约 15m41s）。见下「已修」。 |
| Adaptive 三文件（补 `httpx2` 后，2026-10-23） | `pytest tests/test_adaptive_runtime_v2.py tests/test_adaptive_benchmark.py tests/test_adaptive_linux_sandbox.py -q` → **35 passed, 3 skipped**（约 43s）。3 skip 为 Windows 上 `test_adaptive_linux_sandbox`（需非 root Linux + cgroup），属预期。 |
| **裸全量（不 ignore Adaptive）** `python -m pytest -q --tb=line`（2026-10-23，HEAD `3821b0e`，autopilot，Windows） | **1798 passed**, **32 skipped**, 764 subtests passed（约 15m26s）。**0 failed**。含 Adaptive 三文件；Windows 上 linux sandbox 等仍为 skip。 |
| 记忆接入后（工作树，相对 `3821b0e`）`test_memory_context` + `test_memory` + `test_research_runtime` | **29 passed**（约 26s）。 |
| 可信查新（scoop_context + literature_context + research_runtime + campaign_nodes，相对 `a037d14`） | **61 passed**（约 39s）。跳过≠查新完成；非 provisional scoop 可引用；空/仅 provisional 拒绝「无撞车」类结论。 |

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

### Adaptive / httpx2（已收口）

- `capabilities.agent` 探测含 `httpx2`；`adaptive_runtime` → `pydantic_planner` → `httpx2`。
- 已在 `pyproject.toml` 的 `agent` 与 `adaptive` optional-dependencies 声明 `httpx2>=2.14,<3`；本机 `pip install -e ".[agent]"`（另已单独装过 httpx2）。
- 三文件跑测见上表；linux sandbox 在 Windows 上 skip，未强改。

## 本轮契约与跳过语义（摘要）

- 默认产品路径：已登记研究 → ResearchController + ResearchRuntime。
- `popper adaptive`：描述性基准，默认不开 live。
- `research campaign`：编排图；未真正执行 → `outcome=skipped`（含 dispatch / propose / variant / 缺配置的 materialize）；已有产物幂等复用仍 `success`。
