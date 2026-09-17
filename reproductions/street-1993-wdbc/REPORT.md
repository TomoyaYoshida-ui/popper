# Street et al. (1993) WDBC claim 复现报告

## 结论

**接近复现，但不是算法完全复现。** 论文报告三特征、单线性分离平面的 10 折交叉验证准确率为 **97.00%**。预注册的线性规划近似实现得到 **96.49%**，相差 **-0.51 个百分点**，落在执行前规定的 ±2 个百分点容差内。

论文：[University of Iowa 记录](https://iro.uiowa.edu/esploro/outputs/conferenceProceeding/Nuclear-feature-extraction-for-breast-tumor/9984380474002771) · [DOI](https://doi.org/10.1117/12.148698)  
数据：[UCI Breast Cancer Wisconsin (Diagnostic)](https://archive.ics.uci.edu/dataset/17/breast+cancer+wisconsin+diagnostic) · DOI `10.24432/C5DW2B`

## 对照结果

| 方法 | 特征 | 10 折 pooled accuracy | 与论文差异 |
|---|---:|---:|---:|
| 论文 Street et al. (1993)，MSM-T/RLP | 3 | 97.00% | — |
| 预注册 LP surrogate | 3 | 96.49% | -0.51 pp |
| Linear SVM | 3 | 96.84% | -0.16 pp |
| Linear SVM | 30 | 97.01% | +0.01 pp |

LP surrogate 的 30 次重复 10 折平均准确率为 **96.50%**，重复间标准差 **0.10%**，经验 2.5%–97.5% 分位区间为 **96.31%–96.66%**。

## 与原论文的一致性

- 数据：UCI 官方 WDBC 原始文件，569 个病例、30 个特征、212 个恶性和 357 个良性样本。
- 特征：mean texture、worst area、worst smoothness，与论文摘要一致。
- 验证：10 折分层交叉验证；每个病例恰好作为验证样本一次。
- 决策边界：单一线性平面。
- 不完全一致：原始 fold 分配、MSM-T 软件和 robust linear programming 参数未公开。本复现使用明确记录的 L1 正则 soft-margin LP surrogate，并在每折训练数据内拟合标准化。

因此，本实验支持“这三个特征配合一个线性分离平面可以在该数据集上获得约 97% 的 10 折准确率”，但不能证明原 MSM-T 程序被逐项复现。

## 可审计产物

- `protocol.json`：执行前冻结的特征、模型、折分和判断阈值。
- `source/wdbc.data`：UCI 原始数据，SHA-256 `d606af411f3e5be8a317a5a8b652b425aaf0ff38ca683d5327ffff94c3695f4a`。
- `predictions.csv`：主实验与 30 次重复实验的逐样本折外预测。
- `results.json`：摘要、环境版本、协议与论文 claim 指纹。
- `verify.py`：不训练模型，从保存预测重新计算准确率和复现状态。

## 边界

这是历史数据上的计算复现，不评价 1993 年图像分割流程、论文后续前瞻性病例结果或当前临床有效性。交叉验证复现结果不能用于临床诊断。
