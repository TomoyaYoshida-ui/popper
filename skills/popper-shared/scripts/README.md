# 确定性脚本契约（实现状态如实标注）

> 铁律：**SLO 的机械保证只认脚本，不认散文**。每个脚本 = 明确的输入/输出/退出码，全部离线、确定性、可单测。

| 脚本 | 用途 | 输入 → 输出 | 退出码 | 状态 |
|---|---|---|---|---|
| `validate_results.py` | results.json vs metrics.json 注册表校验 | `results.json`, `metrics.json` → 校验报告 | 0 通过；2 未知指标名/缺字段；3 schema 错 | ✅ 已有最小实现 |
| `audit_claims.py` | claim↔evidence 对齐 | `claims.csv`, `manuscript.md` → 对齐报告（未验证/悬空 claim） | 0/2 | 契约定 |
| `check_consistency.py` | 跨章节数值冲突 | `consistency_manifest.json` → 冲突清单 | 0/2 | 契约定 |
| `check_references.py` | 引用语法/重复 ID | `manuscript.md` → 问题清单 | 0/2 | 契约定 |
| `verify_citations.py` | 四索引引用核验 | `source_manifest.json` → `lookup_verified{true,false,unresolvable}`（S2+OpenAlex+Crossref+arXiv；`verification.db` 90 天缓存；false 仅限 ID-keyed unmatched） | 0/2 | 契约定（借鉴 ARS，CC-BY-NC 重写） |
| `novelty_anchor.py` | 多类别 novelty 分类 + 近邻文献 | `domain_evidence.json`, `idea_card.yaml` → `{novelty_category, novelty_hint, neighbors:[文献id]}` | 0/2 | 契约定 |
| `validate_idea_card.py` | 创新点卡 schema 校验 | `idea_card.yaml`, `domain_evidence.json` → 校验报告（derived_from 未命中 = 拒绝） | 0/2 | 契约定 |
| `stats_check.py` | 预注册统计检验 | `results.json`, `preregistration.yaml` → bootstrap/CV、BH-FDR、paired test 报告 | 0/2 | 契约定 |
| `lint_manuscript.py` | 裸数字/占位符/未验证 claim 拦截 | `manuscript.md`, `claims.csv` → 拦截清单（裸数字 = 拒绝） | 0/2 | 契约定（借鉴 scientific-writing，MIT） |
| `heldout_guard.py` | 密封测试集守门 | 文件路径白名单 → 触碰 held-out 即写 `heldout_read` 审计事件并拒绝 | 0/2 | 契约定 |

## 唯一已实现样例：validate_results.py

```python
#!/usr/bin/env python3
"""results.json vs metrics.json 注册表校验。exit 0=通过 2=未知指标/缺字段 3=schema 错"""
import json, sys

def main(results_path: str, metrics_path: str) -> int:
    try:
        results = json.load(open(results_path, encoding="utf-8"))
        metrics = json.load(open(metrics_path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"schema error: {e}"); return 3
    registered = {m["name"] for m in metrics.get("metrics", [])}
    required = ("name", "value", "mean", "std", "n_seeds", "per_seed")
    errors = []
    for r in results.get("entries", []):
        missing = [k for k in required if k not in r]
        if missing:
            errors.append(f"{r.get('name','?')}: 缺字段 {missing}")
        elif r["name"] not in registered:
            errors.append(f"{r['name']}: 未知指标名（不在注册表）")
    if errors:
        print("\n".join(errors)); return 2
    print(f"OK: {len(results.get('entries', []))} 条全部命中注册表"); return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
```

其余脚本按上表契约逐步实现；每实现一个，配最小单测（构造集：注入 1 个未知指标、1 个裸数字、1 条幻觉引用，必须被拦）。
