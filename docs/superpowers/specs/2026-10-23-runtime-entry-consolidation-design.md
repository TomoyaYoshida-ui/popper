# 科学执行权硬收敛

日期：2026-10-23（演示冻结续写至 2026-10-24）  
状态：**已冻结演示口径** — 确认闸 + 证据闸 + Adaptive/Campaign 科学旁路关闭 + 默认离线演示/`status_brief`  
仓库：`research-agent-v2` / 分支 `autopilot`（至 `fdd0d0e`）

## 已完成

1. **确认集**：`claims.confirm` / `confirm_signed` → `authorize_confirmation`
2. **证据写入**：`record_judgment`、终态 supported/contradicted → `authorize_evidence_write`
3. **Adaptive**：`_run` → `scientific_execution.execute_adaptive_candidate_run`；`_evaluate` 发布 → `publish_paired_mae_result`
4. **Campaign**：`dev` / `freeze` / `confirm` 一律要求 `research-runtime/v1` + `ResearchController`；禁止 `Experiment.search/freeze/confirm`
5. **统一入口**：`ScientificRuntime`；Orchestrator 不是科学运行时
6. **默认演示**：`popper demo smoke`；CLI `--brief` 与工作台 `/api/research.status_brief` 同源

## 仍保留

- Adaptive 类本身仍存在（描述性基准夹具），但执行与计分不在类内私自完成
- idea_node 等非科学步骤可读 Experiment 规格（只读契约），不跑开发/确认
- 不把三套类物理揉成一个文件

## 故意不开

审计级 scoop FPR、第三方技能库、开放写论文、跨周长程记忆、三 runtime 物理并类。
