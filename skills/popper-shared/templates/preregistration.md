# 预注册表模板（popper-design 产出，冻结后改动需用户审批留痕）

```yaml
project_id: P-001
hypotheses:
  - id: H1
    statement: …                    # 可证伪
    derived_from: ["IC-001"]       # 创新点卡 id
metrics:                            # 注册表 → metrics.json
  - name: ours_f1                   # 规范名，results.json 必须使用
    definition: …
baselines: ["bm25", "dpr"]
data:
  dataset_url: …
  held_out_split: …                 # 密封测试集：代码不可写、只经受控 evaluator 读取
  leakage_notes: …
statistical_plan:
  primary_metric: ours_f1
  test: paired bootstrap over test set
  alpha: 0.05
  correction: BH-FDR               # 搜索跨度多重比较
  effect_threshold: 相对≥2% 或成本降≥20%
  n_seeds: 3
  tolerance: 1e-3                  # 复现重跑容差
budget:
  search_rounds: 5                 # K
  tokens_cap: …
frozen_at: …                       # 冻结后任何修改 = 版本 +1 + 用户审批留痕
```

**铁律**：预注册表不完整（缺 primary_metric / held_out_split / correction）→ L0 拒绝进入 code-generate。
