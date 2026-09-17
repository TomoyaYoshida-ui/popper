# 创新点卡模板（popper-ideate 产出 / popper-scoop-check 输入）

```yaml
id: IC-001
type: 选题 | 方法 | 发现          # 三选一
title: 一句话标题
motivation: 为什么要做               # 必须锚定领域语料记录
derived_from: ["DE-012"]           # popper-domain-evidence 的记录 id，必填
operator: replace-module | cross-domain-transfer | counterfactual-mechanism | data-deficit-to-task | negative-result-pivot | 直接派生
pattern: P3                        # 15 类 ideation pattern 编号（非 ML 域可标 N/A）
method: 怎么做（可证伪的最小实验设计）
novelty_category: 新问题 | 新数据 | 新方法族 | 新连接 | 增量delta
novelty_hint: 重大 | 增量 | 平凡    # provisional，最终由 scoop-check + 用户裁决
```

**硬校验**（`validate_idea_card.py`）：
- `derived_from` 非空且全部命中 `domain_evidence.json` 已有记录，否则 L0 拒绝；
- `type` ∈ 三选一；`novelty_hint` ∈ 三选一；
- `method` 必须可证伪（含"什么结果算支持/什么结果算证伪"一句）。
