---
name: popper-package
description: 物化与复现包（自研，5 套库无此能力）。把稿件与全部产物打包成 docx/pdf + 一键复现 zip（代码 + 数据 + 锁文件 + 重跑脚本），并做可重跑 smoke 验证。当用户要"出终稿 + 投稿用复现包"时使用。
license: 自研
metadata:
  version: "1.0"
  skill-author: Popper
  sources: 自研
---

# popper-package — 物化与复现包

## 什么时候用

- 预检通过后，出终稿 + 复现包；
- 任何"别人要能一键重跑我结果"的场合（投稿/答辩/rebuttal）。

## 铁律

1. **复现包必须真的能重跑**：打包后做一次干净环境 smoke（重跑 → 比对 results.json 与稿中数字），跑不通 = L0 拒绝，禁止"打包了但没人跑过"；
2. **环境锁三要素齐全**：依赖版本（lockfile）+ 平台指纹（OS/CPU/指令集）+ 线程控制（BLAS/OMP 固定）；
3. 全部产出 sha256 登记入 `files` 表。

## 工作流

1. **稿件物化**：paper.json → docx/pdf（venue 模板），数字/引用保留 claim/ref 下钻链接（或脚注映射表）；
2. **复现包**：`reproducibility.zip` = 代码 + 数据（或数据获取脚本）+ lockfile + 平台指纹 + 种子配置 + `run_all.sh`（一键重跑）+ `README`（怎么跑、预期结果、容差）；
3. **可重跑 smoke**：干净环境跑 `run_all.sh` → 比对 results.json vs 稿中 claim 数字（≤ 预注册容差）→ 记录 smoke 报告；
4. **登记**：sha256 入库；复现包与稿件版本关联。

## 确定性脚本

- smoke 比对脚本（results.json ↔ claims.csv 数值比对，容差来自预注册表）；
- zip 内容清单校验（四要素齐全检查）。

## 边界

- 复现 = 同环境重跑一致（SLO-3），不承诺跨平台逐位一致（平台差异如实报告）；
- pptx/html 等衍生格式非主线，按需降级。
