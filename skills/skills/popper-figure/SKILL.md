---
name: popper-figure
description: 科研图（支撑 skill）。生成投稿级数据图与示意图：matplotlib/seaborn 数据图、示意图草稿、venue 风格适配、颜色无障碍。当用户要"把结果画成投稿级图"时使用。
license: Apache-2.0 / MIT（整合 nature-skills nature-figure、AI-Research-SKILLs academic-plotting）
metadata:
  version: "1.0"
  skill-author: Popper
  sources: nature-skills (nature-figure), AI-Research-SKILLs (academic-plotting)
---

# popper-figure — 科研图

## 什么时候用

- `popper-write` 需要图：数据图（曲线/箱线/CDF/消融表）+ 示意图（架构/流程）；
- 已有图要按 venue 风格重排。

## 铁律

1. **图的数据来自 results.json**（per_seed 画误差线），不手绘"看起来合理"的数据；
2. **图注与正文一致**：误差线定义（std/SE/CI）、样本量、检验方法写进图注，并与 `check_consistency.py` 的数值底账对齐；
3. 颜色无障碍（colorblind-safe palette），字体/尺寸按 venue 规范；
4. 示意图（LLM 生成）必须标"草稿，需人工核对"，不直接当终稿。

## 工作流

1. 数据图：results.json per_seed → matplotlib/seaborn，按 venue 样式（字号/线宽/配色/图注格式）；
2. 示意图：自然语言 → 草稿（构图 → 人工确认 → 细化）；
3. 每图落 `figure_manifest`：数据来源 N-ID、误差线定义、与正文引用位置一一对应。

## 边界

- 图是"表达"，数据是"执行"——数据改，图必须重画（不手改）。
