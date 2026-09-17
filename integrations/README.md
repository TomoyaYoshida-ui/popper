# 开源组件集成

Popper 将工作区外层开源项目作为运行时能力供应方，自身负责统一编排、状态、门禁、证据和产品界面。供应方源码不复制进本项目；`vendors.json` 固定实际调用文件、许可证和 SHA-256。

## 当前链路

`ResearchStudio-Idea → paper-search-v1 → scoop-check-v1 → Arbor hypothesis tree → Popper dev evaluator`

- `idea-next-v1` 调用真实 `run.py next`，返回结构化导航和原始回执。
- `arbor-tree-v1` 调用真实 `tree.py`，支持初始化、观察、校验、建点、证据回写、洞见传播、剪枝、merge 和 cycle。
- `idea-to-arbor-v1` 优先读取 `phase3_revise/final_candidate.json`，其次读取 `phase2_coherence/refined_candidate.json` 和 `phase2_generate/phase2_generate_output.json`。它把标题和可证伪预测写成假设节点，并在 Arbor 运行目录的 `.popper-integration/idea-arbor-links.json` 保存来源与指纹。
- `paper-search-v1` 通过隔离 worker（`popper.vendor_worker`）调用 ResearchStudio 的 `dedup` 和 `rank`，并转发其 `_start_worker`/`_collect_worker` 启动分来源子进程、施加每来源 wall-clock deadline；保存分来源原始结果、去重排序结果、告警、请求指纹和检索时间。worker 不替换供应方模块的任何函数。
- `scoop-check-v1` 按 ResearchStudio 的七步协议保存 `step1.json` 至 `step7.json`，对候选做四轴分解、摘要初筛、全文核验、Level 1–5 判定和 delta 生成。
- `scoop-to-arbor-v1` 把 verdict、closest paper、delta、检索哈希和全文哈希绑定到 Arbor node。`provisional` 默认被门禁拒绝。
- `arbor-popper-dev-v1` 将一个 pending node 映射到 `experiment.json` 中的预注册候选索引，调用 Popper 控制器完成 baseline/candidate 开发集计分，再把分数、方向统一的 delta 和 Popper run ID 写回 Arbor evidence。
- `arbor-dispatch-v1` 让 BYOK 模型在 Idea、Scoop delta 与预注册候选之间做受限映射。模型可以选择一个候选，也可以返回 `not_implementable`；它不能产生新配置或代码。
- `autoresearch-snapshot-v1` 复用 AI-Research-SKILLs 的双循环日志与 findings 协议，把 Arbor 节点、Popper 指标、运行 ID、洞见和 frontier 投影为可持续研究记忆。
- `code-proposal-v1` 使用已固定指纹的 AI-Research-SKILLs ML training recipe 作为实现指导，只允许对已登记 `code_files` 提交完整替换，并产出哈希绑定的 review diff。它不修改或执行工作项目。
- `code-variant-v1` 在显式 `--approved` 后把 proposal 物化到隔离目录，保留 baseline/candidate 两套代码并生成统一路由入口，使 Popper 能在相同数据、配置、种子和指标下比较代码差异。

## 约束

- 所有运行目录必须位于 `integrations/runs`。
- `vendors.json` 的 `source_root` 形如 `../../X-main`，指向**与 `research-agent-v2/` 同级**的外层目录。只克隆本仓库而不把上游仓库放在同级时，`vendor inspect` 会如实报「开源组件目录缺失或越界」——这是设计内的 fail-closed，不表示集成失效。注册表本身的契约（指纹不匹配、许可证/SKILL/入口未锁定、路径越界等）由 `tests/test_vendors_registry.py` 用仓库内合成 fixture 在任何机器上验证，真实上游语料上的用例则按可用性门控 skip。
- 每次调用前校验供应方文件指纹；源码变化需要人工复核并更新登记。
- 当前来源是没有 `.git` 的压缩包解压目录，因此登记文件 SHA-256，不声称存在 commit pin。
- 组件子进程只继承最小环境变量，但这不是操作系统沙箱。
- `paper-search` 与 `scoop-run` 在非沙箱路径执行供应方代码，因此必须显式传 `--trusted-local`；campaign 的 `literature`/`scoop` 节点只在 `mode=trusted_local` 时执行，其余模式跳过并标注原因。
- Arbor 的 merge 原语不能重复暴露 Popper 的最终测试反馈；最终测试仍受 Popper 一次性消费状态机约束。
- Arbor 执行适配器只调用 Popper `dev` evaluator；不提供调用 `test` 的参数。实验必须处于 `searching`，Arbor 与 Popper 的指标方向必须一致。
- Scoop 的语义步骤使用用户配置的 OpenAI-compatible BYOK 模型。没有模型配置仍可独立运行 paper-search。

## 检查

```powershell
python -m popper vendor inspect
python -m unittest tests.test_vendors -v
```
