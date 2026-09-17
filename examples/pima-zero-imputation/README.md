# Pima Zero-Imputation · M2 真实研究试点报告

更新：2026-09-11

## 研究问题与预注册

在 UCI Pima Indians Diabetes（768 例，8 特征，二分类）上，检验「把生理上不可能的零值（缺失值）做**中位数插补**，是否比把零当有效值更能提升诊断准确率」。

- 数据源：GitHub 镜像（UCI 官方旧版 URL 已下线），固定 SHA-256 校验，分层 60/20/20 划分，`random_state=7`。
- 指标 `accuracy`(max)，seeds=[11,29,47]，budget=4，min_improvement=0.02。
- 预注册配置：baseline = 不插补+逻辑回归；candidates = 中位数插补 LR(C=1|0.01)、中位数插补 RF、不插补 RF。

## 结果（开发集，控制器重算，三种子取均值）

| 配置 | accuracy |
|---|---|
| baseline: none + LR(C=1) | 0.7662 |
| median + LR(C=1) | 0.7597 |
| median + LR(C=0.01) | 0.7468 |
| median + RF | 0.7684 |
| **none + RF（冻结）** | **0.7900** |

最终确认（测试集）：断言 delta = **0.0216**，过 0.02 阈值，claim C001 = supports_threshold。

## 结论（负面——如实标注）

**研究主假设「中位数插补提升准确率」未获开发集支持**：best 配置是不插补+RF（0.7900），而非中位数插补。针对插补的直接对照（LR：median 0.7597 vs none 0.7662）orienterd_delta 为 **-0.0065（插补反而略降）**。这一结果是单一固定划分上的描述性工程验证，未做显著性检验，不声明医学有效性或机制创新。

## Agent 闭环执行链（真实 SaaS 证据）

- **Idea 候选**：ResearchStudio 导航器输出 Phase0 多轮真实检索流程（需自跑 LLM 子任务），本次按计划以真实 LLM 补齐 canonical candidate —— **半自动**。
- **论文检索**：ResearchStudio paper_search 真实多源回执，82 篇去重相关性排序，命中缓存 `search-*.json`（本次 miss，已缓存）—— 自动。
- **Scoop 查新**：七步状态机真实执行，最终 `provisional`（step5 全文不可达，`fulltext_sha256=[]`）；closest=PE_DIM（同样处理 Pima 缺失值但用模型式插补），level=3 Medium Overlap —— 自动，但全文级核验未完成。
- **Arbor 树**：真实假设树初始化 + n1 节点绑定 scoop 证据，`scoop-to-arbor`（显式 --allow-provisional）—— 自动。
- **候选映射**：arbor-dispatch 真实 LLM（deepseek-chat）只从已注册候选选 index=0（median-LR，正命中 Idea 假设），执行开发集评估并回写证据 —— 自动。
- **确定性实验**：Popper freeze→confirm（none+RF 被冻结），replay 一致（7 runs）—— 自动。

## 自动化边界（如实标注）

- **已全自动**：论文检索、Scoop 七步状态机（断点恢复）、Scoop/Arbor 证据回写、arbor-dispatch 候选映射、Popper 实验/冻结/确认/重放。
- **半自动**：Idea Spark 的 LLM 子任务（Phase0 检索 → Phase3 候选）未被 Popper 提供模型执行器，本次用真实 LLM 补齐 candidate；全过程未虚称「Idea 全自动」。
- **未完成 / 未启用**：容器隔离（本次用 `--trusted-local`）；Scoop 全文级核验（网络/DOI 不可达 → provisional）；统计显著性检验（仅描述性）；三方对照（人工/预注册/Agent）——留待下一步。

## 产物位置

- 实验项目：`examples/pima-zero-imputation/`（experiment.json / prepare_data.py / model.py / 三集 / dataset_source.json / .popper/）
- Agent 闭环回执：`integrations/runs/pima/{idea,scoop,arbor}/`
- 研究快照：`integrations/runs/pima/arbor/.popper-integration/autoresearch/{snapshot.json,research-log.md,findings.md}`
- Popper 报告：`examples/pima-zero-imputation/.popper/report.md`

## 实施中对 Popper 代码的最小修复（阻断性）

1. `popper/scoop.py` step1/step3/step6 prompt 显式给定固定四轴名称（否则真实 LLM 会自创轴名导致契约校验失败）。
2. `popper/scoop.py` **make_json_client** `max_tokens` 4000→8000；`deepseek-flash` 是推理模型会把预算花在 reasoning 上截断 content，结构化 JSON 步骤改用非推理的 `deepseek-chat`。
3. `popper/scoop.py` step3 增加 `TRIAGE_CAP=30`：论文过多时单次 triage 输出超模型上限被截断，故取相关性 Top-N（保留 step2 全量）。