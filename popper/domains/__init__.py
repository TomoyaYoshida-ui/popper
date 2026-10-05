"""领域包注册表。

导入本包即触发内置域包的注册（导入副作用），``popper.core`` 依赖这一副作用构建
``EVALUATORS``、``dataset``、``score`` 等判定入口。新增内置指标域包为两步：

1. 在 ``popper/domains/`` 下新增一个模块，定义并 ``register()`` 一个域包；
2. 在下方内置清单里加一行导入。

``popper.core`` 不能在模块级导入本包（域包需要 core 的校验助手），因此 core 以
惰性视图暴露 ``EVALUATORS``：首次访问时才导入这里。

外部域包可通过 ``popper.domain_packs`` entry point 提供单例对象，并用
``POPPER_DOMAIN_PACKS`` 显式选择要加载的 entry point 名称。域包代码拥有当前
Python 进程权限，因此绝不自动加载所有已安装插件。
"""
from importlib.metadata import entry_points
import inspect
import os

from . import protocol as _protocol

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
from . import oscillator_trajectory  # noqa: F401  trajectory scoring; not resource attestation
from . import treatment_effect  # noqa: F401  注册 treatment-effect-v1
from . import mlebench_lite  # noqa: F401  注册三道 MLE-bench Lite 小题


def load_external_packs(selection=None):
    """Load explicitly trusted installed packs; fail closed on ambiguity or absence."""
    selected = os.environ.get("POPPER_DOMAIN_PACKS", "") if selection is None else selection
    if not isinstance(selected, str):
        raise ValueError("POPPER_DOMAIN_PACKS 必须是逗号分隔的 entry point 名称")
    names = [name.strip() for name in selected.split(",") if name.strip()]
    if not names:
        return ()
    if len(set(names)) != len(names):
        raise ValueError("POPPER_DOMAIN_PACKS 包含重复名称")
    available = {}
    for point in entry_points(group="popper.domain_packs"):
        available.setdefault(point.name, []).append(point)
    loaded = []
    try:
        for name in names:
            matches = available.get(name, ())
            if len(matches) != 1:
                raise ValueError(f"外部域包 entry point 不存在或重名: {name}")
            pack = matches[0].load()
            source = inspect.getsourcefile(pack.__class__)
            if source is None:
                raise ValueError(f"外部域包缺少可读取的 Python 实现源码: {name}")
            distribution = getattr(matches[0], "dist", None)
            if distribution is None:
                raise ValueError(f"外部域包缺少可核对的发行包元数据: {name}")
            register(pack, source_path=source, distribution=distribution)
            loaded.append(pack.evaluator_id)
    except Exception:
        # Selection is one operation: a later missing/invalid pack must not
        # leave earlier evaluator identities visible to the Research Kernel.
        for evaluator_id in loaded:
            _protocol._PACKS.pop(evaluator_id, None)
            _protocol._EXTERNAL_SOURCES.pop(evaluator_id, None)
            _protocol._EXTERNAL_DISTRIBUTIONS.pop(evaluator_id, None)
        raise
    return tuple(loaded)


load_external_packs()

__all__ = [
    "ALIGNED_PREDICTION", "DEFAULT_SPLITS", "IMPLEMENTED_SHAPES", "INPUTS_SPLIT",
    "STAGED_ARTIFACTS", "DomainPack", "MetricSpec", "UnsupportedEvaluator", "get",
    "metric_spec", "packs", "primary_metric", "register", "registered_evaluators",
    "registered_metrics", "load_external_packs",
]
