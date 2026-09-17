# 整合映射表 — 5 套库 → 每阶段一条 skill

> 说明：`canonical` = 该阶段的主来源；`借鉴` = 只取思路重写（CC-BY-NC 或语义不匹配）；`自研` = 5 套库都没有、必须自己写（这正是"自己的一套东西"里真正属于你的部分）。

| 阶段 | 整合后 skill | canonical 来源 | 取舍决定 | 许可证 |
|---|---|---|---|---|
| 0 摄入 | `popper-ingest` | scientific-agent-skills `liteparse`/`markitdown` + nature-skills `nature-reader` | 三合一：解析/OCR 用 liteparse+markitdown，图文对照阅读用 nature-reader；引用抽取进 search 复核 | MIT / Apache-2.0 |
| 1 检索 | `popper-search` | ResearchStudio `paper_search`（六源去重）+ scientific `paper-lookup` | 合并六源检索（arXiv/DBLP/OpenAlex/OpenReview/S2/Crossref），锚定 `source_api + raw_response_id` | MIT |
| 2 领域语料 | `popper-domain-evidence` | **自研**（复用 search 输出） | 5 套库无此能力；gap/矛盾/负结果/前瞻信号四类记录，每条锚定文献 id | 自研 |
| 3 选题 | `popper-ideate` | ResearchStudio `idea_spark`（15 类 pattern）+ AI-Research `brainstorming-research-ideas`/`creative-thinking-for-research` | 15 类 pattern（ML 域直接用，非 ML 域复用方法学）+ 自研提案算子（replace-module/cross-domain-transfer/counterfactual-mechanism/data-deficit-to-task/negative-result-pivot） | MIT / 自研算子 |
| 4 查新 | `popper-scoop-check` | ResearchStudio `scoop_check` + ARS 查新反证（借鉴） | Scoop-Check 做 per-axis prior-art；对抗反证借鉴 ARS 思路重写 | MIT / 自研反证 |
| 5 设计 | `popper-design` | scientific `hypothesis-generation` + **自研预注册表** | 证据锚定假设 + 预注册（指标注册表/检验计划/α/BH-FDR/效应量/held-out 切分） | MIT / 自研模板 |
| 6 闭环 | `popper-execute` | scientific `arbor`（HTR） | vendor 方法论 + `tree.py` 数据结构；executor 隔离改成自研 REE 容器；`results.json` 权威通道自研 | MIT / 自研 |
| 7 证据 | `popper-evidence` | scientific `scientific-writing`（claim registry）+ ARS 四索引 gate（借鉴）+ nature `nature-ref-verifier` | **claim registry 为全流程统一表示**；引用核验 = 四索引 gate（借鉴重写）+ 字段级校验（vendor，含中文 CNKI/万方） | MIT / CC-BY-NC(借鉴重写) / Apache-2.0 |
| 8 写作 | `popper-write` | scientific `scientific-writing` + nature `nature-writing`/`nature-polishing` + AI-Research `ml-paper-writing` | 数字经 claim 通道 + 裸数字拦截；LaTeX 模板取 ml-paper-writing；中文润色取 nature-polishing | MIT / Apache-2.0 |
| 9 审稿 | `popper-review` | ARS `academic-paper-reviewer`（借鉴）+ scientific `peer-review` + nature `nature-reviewer`/`nature-response` | 7-agent 多视角 + Devil's Advocate（借鉴重写）+ rebuttal（nature-response vendor） | CC-BY-NC(借鉴重写) / MIT / Apache-2.0 |
| 10 预检 | `popper-verify` | ARS 7-mode（借鉴）+ **自研 R1–R7** | 双层：拒稿侧 R1–R7（自研分类树）+ 造假侧 7-mode（借鉴重写）；报告分两栏 | CC-BY-NC(借鉴重写) / 自研 |
| 11 物化 | `popper-package` | **自研** | 5 套库无"复现包打包"能力；docx/pdf 模板 + 一键复现 zip（代码+数据+锁+重跑脚本） | 自研 |
| 支撑 | `popper-stats` | nature `nature-statistics` + scientific `statistical-analysis`/`experimental-design`/`statistical-power`/`uncertainty-and-units` | 合并统计审查清单（实验单位/重复数/p 值/多重比较/效应量/CI/图注统计） | Apache-2.0 / MIT |
| 支撑 | `popper-figure` | nature `nature-figure` + AI-Research `academic-plotting` | 合并投稿级科研图工作流 | Apache-2.0 / MIT |

## 去重结论（同义 skill 只留一条）

- **文献综述类**：ARS `deep-research`、scientific `literature-review`、nature `literature-pipeline`、AI-Research autoresearch → 只留 `popper-search` + `popper-domain-evidence`，其余丢弃（综述是 search + domain-evidence 的产物，不是独立阶段）。
- **引用校验类**：ARS citation gate、nature `ref-verifier`、scientific `citation-management`/`pyzotero`/`paper-lookup` → 收敛进 `popper-evidence`（四索引 + 字段级 + Zotero 通道）。
- **写作类**：scientific `scientific-writing`、nature `writing`/`polishing`、AI-Research `ml-paper-writing`/`systems-paper-writing`、ARS `academic-paper` → 收敛进 `popper-write`（claim 通道 + 中英润色 + LaTeX 模板）。
- **审稿类**：ARS `academic-paper-reviewer`、scientific `peer-review`、nature `reviewer`/`response` → 收敛进 `popper-review`。
- **idea 类**：ResearchStudio `idea_spark`、AI-Research `brainstorming`/`creative-thinking`、scientific `hypothesis-generation`/`scientific-brainstorming` → 收敛进 `popper-ideate` + `popper-design`。

## 三个"5 套库都没有、必须自研"的护城河（即这套东西里真正属于你的）

1. **`popper-domain-evidence`** —— CS 领域语料（gap/矛盾/负结果/前瞻信号库）；
2. **`popper-package`** —— 一键复现包；
3. **`popper-verify` 的 R1–R7 分类树** —— 拒稿理由语料（PeerRead/OpenReview 归纳 + 双人标注）。

> 这三条正好对应"发 paper 最重要"的三件事里机器能保证的部分：**证据链（domain-evidence）→ 可复现（package）→ 守得住（verify）**。其余 skill 是白菜价的地基，这三条是你这套东西的灵魂。
