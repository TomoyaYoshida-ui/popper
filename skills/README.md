# Popper Skills — 面向计算机大类发 paper 的整合科研 skills

> 把你工作区的 5 套开源 skills 去重、调和、整合成**一套自洽的科研 skill 集**：面向计算机大类，主线是"找到创新点 + 发 paper"，统一证据表示，统一 frontmatter，许可证逐条标注。

## 一句话定位

**一套把「找到可发表创新点 → 反证查新 → 闭环验证 → 写成能投的论文」串成流水线的 Agent Skills，每一步对应一条 skill。**

## 实现状态（如实标注，别把这些当已完成能力）

- 本目录是**设计规格**（SKILL.md 写下何时用、怎么走流程、该调哪个脚本），**不是 Popper 运行时的一部分**：`popper/**` 生产代码与测试都不读取本目录，把它装进 Claude Code / Codex 等运行时即可作为独立 skill 集使用。
- `popper-shared/scripts/` 除 `validate_results.py`（最小实现，见该目录 README 内联代码）外，其余脚本**尚未实现**；SKILL.md 里对脚本的引用是契约，不是既有能力。
- Popper 内核已有等价或更严的实现（`popper/research/`：契约校验、门禁、证据、统计、隔离），二者口径以代码与 `docs/技术方案.md` 为准；本目录不构成第二套权威。

## 整合的三条原则（解决 5 套库"互不相容"的问题）

1. **统一证据表示 = claim registry**：所有 skill 之间传递证据，一律用 scientific-writing 的 claim registry（C/N/M/O/R 四类 ID + `source_manifest.json`/`claims.csv`/`consistency_manifest.json`）。不混用 ARS 的 Material Passport、nature 的 proposal-first 状态机——**一套表示，全流程通用**。
2. **确定性逻辑抽成脚本，不留在散文里**：引用核验、claim 校验、统计检验、结果 schema 校验，全部指向 `popper-shared/scripts/` 的确定性脚本；SKILL.md 只写"什么时候用、怎么走流程、哪里调脚本"。
3. **许可证逐条标注**：每一条 skill 的 frontmatter 都标 license 与来源；CC-BY-NC（ARS）的内容**仅借鉴思路、重写实现，不复制原文**，MIT/Apache-2.0 可整合。

## 流水线（每条对应一条 skill）

```
popper-ingest   稿件摄入（PDF/tex/md + OCR + 引用抽取）
      ↓
popper-search   文献检索（六源去重，锚定原始回执）
      ↓
popper-domain-evidence  领域语料（gap/矛盾/负结果/前瞻信号）
      ↓
popper-ideate   创新点生成（15 类 pattern + 提案算子 + 创造性思维）
      ↓
popper-scoop-check  反证查新（per-axis prior art + provisional novelty）
      ↓
popper-design   研究设计 + 预注册（指标注册表/检验计划/held-out）
      ↓
popper-execute  实验闭环（HTR + held-out merge gate，results.json 权威通道）
      ↓
popper-evidence 证据系统（claim registry + 引用核验 + 一致性 lint）
      ↓
popper-write    论文写作（数字经 claim 通道，引用带锚点）
      ↓
popper-review   对抗性审稿 + rebuttal
      ↓
popper-verify   拒稿预检（R1–R7 + 7-mode 造假侧）
      ↓
popper-package  物化（docx/pdf + 一键复现包）
```

支撑性 skill（不占主流程，按需调用）：`popper-stats`（统计审查，对应 R5）、`popper-figure`（科研图）。

## 目录结构

```
skills/
├── README.md              ← 本文件
├── INTEGRATION_MAP.md     ← 5 套库 → 每阶段的取舍映射表
├── popper-shared/        ← 共享：claim registry schema + 确定性脚本 + 预注册模板
│   ├── schema/            ←   source_manifest / claims / consistency_manifest JSON schema
│   ├── scripts/           ←   audit_claims / check_consistency / check_references / 四索引引用核验 / 统计检验
│   └── templates/         ←   预注册表、创新点卡、拒稿风险报告模板
└── skills/
    ├── popper-ingest/SKILL.md
    ├── popper-search/SKILL.md
    ├── popper-domain-evidence/SKILL.md
    ├── popper-ideate/SKILL.md        ← 已写好（样板）
    ├── popper-scoop-check/SKILL.md
    ├── popper-design/SKILL.md
    ├── popper-execute/SKILL.md
    ├── popper-evidence/SKILL.md
    ├── popper-write/SKILL.md
    ├── popper-review/SKILL.md
    ├── popper-verify/SKILL.md
    ├── popper-package/SKILL.md
    └── popper-stats/SKILL.md
```

## 使用方式

装进任意支持 Agent Skills 的运行时（Claude Code / Codex / 等）：

```bash
# Claude Code
/plugin marketplace add <你的仓库>
# 或直接把 skills/skills/* 拷进 ~/.claude/skills/

# 触发（自然语言，按需选阶段）
"把这套 skills 用起来，帮我从这几篇种子论文出发找一个 CS 方向的创新点"
```

## 来源与许可证

| 来源库 | 许可证 | 在本项目中的用法 |
|---|---|---|
| scientific-agent-skills（K-Dense） | MIT | 整合（arbor/scientific-writing/peer-review/statistical-*/liteparse 等） |
| ResearchStudio（Microsoft） | MIT | 整合（idea_spark/paper_search/scoop_check） |
| AI-Research-SKILLs（Orchestra Research） | MIT | 整合（brainstorming/creative-thinking/ml-paper-writing/ara） |
| nature-skills（Yuan1z0825） | Apache-2.0 | 整合（ref-verifier/statistics/writing/reviewer/response） |
| academic-research-skills（Cheng-I Wu） | **CC-BY-NC 4.0** | **仅借鉴思路、重写实现，不复制原文**（四索引引用 gate / 7-mode / reviewer 协议） |

> 本项目**非商用、开源**。整合进 CC-BY-NC 来源的 skill，其 frontmatter 标注 `license: CC-BY-NC-4.0（仅借鉴，重写实现）`；其余标注 MIT / Apache-2.0。详见 `INTEGRATION_MAP.md`。
