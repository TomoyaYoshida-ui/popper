---
name: popper-domain-evidence
description: 领域语料构建（自研，5 套库无此能力）。从种子论文出发构建某方向的领域证据库：gap/矛盾/负结果/前瞻信号四类记录，每条锚定文献 id。是 popper-ideate 的唯一输入——候选创新点只能从这里派生。当用户要"系统梳理一个方向的空白与矛盾"或准备选题时使用。
license: 自研
metadata:
  version: "1.0"
  skill-author: Popper
  sources: 自研（复用 popper-search 输出）
---

# popper-domain-evidence — 领域语料构建

## 什么时候用

- 用户给几篇种子论文/一个方向，要开始"找创新点"——**这是整个流水线的第一步**；
- 定期增量更新既有领域库。

## 铁律

1. **每条记录必须有文献锚点**（文献 id ∈ 论文库），没有锚点的观察不得入库；
2. **"gap" 必须是可证伪的**：一条 gap 必须能指出"什么实验/结果可以填掉它"，否则降级为"观察"，不进 gap 类；
3. 记录可增量回流：用户后续跑选题/实验产生的新观察，人工筛选后回流入库——**这是本套 skill 越用越厚的数据资产**。

## 四类记录（`domain_evidence.json`）

```json
[{
  "id": "DE-001",
  "type": "gap | contradiction | negative_result | forward_signal",
  "statement": "一句话：什么缺口/什么矛盾/什么负结果/什么新信号",
  "papers": ["E012", "E015"],           // 锚定文献
  "falsifiable": "什么实验能填/证伪它",   // gap 必填
  "confidence": "high | mid | low",
  "source_run": "search_<id>",
  "timestamp": "…"
}]
```

四类来源：
- **gap**：方法族在某任务上最近卡住 / 无成熟基线的空隙；
- **contradiction**：两篇文献结论互斥、或方法与结论不一致；
- **negative_result**：近期被报告的负结果 / 被拒稿的强假设；
- **forward_signal**：刚出现、尚无统一范式的新难题/新数据集（R7 的 importance 前瞻信号来源）。

## 工作流

1. `popper-search` 建论文库（种子 × 引用网络，穷尽式而非 top-k）；
2. 对每篇候选做"可选题价值"通读（重点读 limitation / future work / 被引争议），提取候选记录；
3. **人工筛选**（关键：LLM 提取候选，人裁决入库；一条 1–2 分钟）；
4. 最小可行规模 N≥200 条起步，不追求统计完备，追求"能用 + 可增量积累"；
5. 产出 `domain_evidence.json` + 覆盖声明（哪些子方向已梳理、哪些还空）。

## 确定性脚本

- `novelty_anchor.py` 的检索部分复用（对领域库做近邻/聚类，辅助发现"被忽略的角落"）；
- 记录 schema 校验：type ∈ 四类、papers 非空、gap 必须含 falsifiable 字段。

## 边界

- **领域范围 = 计算机大类**（ML 域权重成熟；非 ML 子领域逐子领域建，A1′ 案例评审把关质量）；
- 本 skill 只产"证据"，不产"选题"——选题是 `popper-ideate` 的事，别越权。
