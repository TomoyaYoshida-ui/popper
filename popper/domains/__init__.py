"""领域包注册表。

导入本包即触发内置域包的注册（导入副作用），``popper.core`` 依赖这一副作用构建
``EVALUATORS``、``dataset``、``score`` 等判定入口。新增一个指标域包固定为两步：

1. 在 ``popper/domains/`` 下新增一个模块，定义并 ``register()`` 一个域包；
2. 在下方内置清单里加一行导入。

``popper.core`` 不能在模块级导入本包（域包需要 core 的校验助手），因此 core 以
惰性视图暴露 ``EVALUATORS``：首次访问时才导入这里。
"""
from .protocol import (ALIGNED_PREDICTION, DEFAULT_SPLITS, IMPLEMENTED_SHAPES, INPUTS_SPLIT,
                       STAGED_ARTIFACTS, DomainPack, MetricSpec, UnsupportedEvaluator, get,
                       metric_spec, packs, primary_metric, register, registered_evaluators,
                       registered_metrics)
from . import tabular_regression  # noqa: F401  注册 mse-v1
from . import binary_classification  # noqa: F401  注册 binary-accuracy-v1
from . import mae_regression  # noqa: F401  注册 mae-v1
from . import binary_f1  # noqa: F401  注册 binary-f1-v1
from . import multiclass  # noqa: F401  注册 multiclass-macro-f1-v1
from . import algorithm_throughput  # noqa: F401  注册 algorithm-throughput-v1
from . import convergence_order  # noqa: F401  注册 convergence-order-v1
from . import treatment_effect  # noqa: F401  注册 treatment-effect-v1

__all__ = [
    "ALIGNED_PREDICTION", "DEFAULT_SPLITS", "IMPLEMENTED_SHAPES", "INPUTS_SPLIT",
    "STAGED_ARTIFACTS", "DomainPack", "MetricSpec", "UnsupportedEvaluator", "get",
    "metric_spec", "packs", "primary_metric", "register", "registered_evaluators",
    "registered_metrics",
]
