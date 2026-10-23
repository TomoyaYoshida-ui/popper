# 科学执行权硬收敛

日期：2026-10-23  
状态：确认闸 + 证据闸 + Adaptive 执行/评估共享模块 + Campaign 科学步骤去 Experiment  
仓库：`research-agent-v2` / 分支 `autopilot`

## 已完成

1. **确认集**：`claims.confirm` / `confirm_signed` → `authorize_confirmation`
2. **证据写入**：`record_judgment`、终态 supported/contradicted → `authorize_evidence_write`
3. **Adaptive**：`_run` → `scientific_execution.execute_adaptive_candidate_run`；`_evaluate` 发布 → `publish_paired_mae_result`
4. **Campaign**：`dev` / `freeze` / `confirm` 一律要求 `research-runtime/v1` + `ResearchController`；禁止 `Experiment.search/freeze/confirm`
5. **统一入口**：`ScientificRuntime`；Orchestrator 不是科学运行时

## 仍保留

- Adaptive 类本身仍存在（描述性基准夹具），但执行与计分不在类内私自完成
- idea_node 等非科学步骤可读 Experiment 规格（只读契约），不跑开发/确认
- 不把三套类物理揉成一个文件
