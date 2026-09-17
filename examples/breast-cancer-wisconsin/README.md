# Breast Cancer Wisconsin 真实 ML 接入

这个项目把 scikit-learn 内置的 Breast Cancer Wisconsin (Diagnostic) 公开数据接入 Popper。数据包含 569 个样本、30 个数值特征和二分类诊断标签。

目的仅是验证 Popper 能处理真实多特征分类、随机种子、标准 ML 依赖和控制器 accuracy 评分。结果不能用于临床诊断，也不构成科研创新。

候选比较 GaussianNB、标准化逻辑回归、随机森林、RBF SVC 和 Extra Trees。数据以固定随机状态按 60%/20%/20% 分为训练、开发和最终测试。开发集用于候选选择，最终测试只在冻结后消费一次。

```powershell
python prepare_data.py
python -m popper experiment init .
python -m popper experiment search . --trusted-local
python -m popper experiment freeze .
python -m popper experiment confirm . --trusted-local
python -m popper experiment replay .
python -m popper experiment report .
```

数据来源、scikit-learn 版本、样本量与划分随机状态保存在 `dataset_source.json`。
