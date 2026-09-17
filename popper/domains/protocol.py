"""领域包协议：一类科研任务的完整接入契约。

Domain Pack = 任务形状 + 数据契约 + 指标集 + 切分策略 + 重复单位。三件套里
``TaskShape`` 决定「候选如何被调用」，``MetricSpec`` 决定「结果如何被判定」，
``DomainPack`` 把两者与数据契约绑成一个可注册单元。

判定边界（决策 1）：``score`` / ``validate_*`` / ``row_identity`` 参与判定，
实现必须只用标准库且可审计；数据加载、可视化、交付渲染等非判定代码可以带依赖。

本模块只用标准库、不导入 popper 其它模块：具体域包需要反向依赖
``popper.core`` 的校验助手与 ``ProtocolError``，若本模块也导入 core 就会成环。
``popper.core`` 因此以惰性视图暴露 ``EVALUATORS``，注册表在首次查询时才构建。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

# 任务形状：决定「候选如何被调用」。
# aligned_prediction：逐样本预测，id 集合必须与输入一致（现有行为）。
# staged_artifacts：执行一次、产出声明式制品（多标量/非逐样本领域）。
ALIGNED_PREDICTION = "aligned_prediction"
STAGED_ARTIFACTS = "staged_artifacts"

# 已实现的任务形状。声明了未实现形状的域包在注册期立即失败，
# 而不是被静默当成逐样本预测处理。
IMPLEMENTED_SHAPES = (ALIGNED_PREDICTION, STAGED_ARTIFACTS)

# 保留的「分割」名：候选可见、不含标签的输入，由 project_inputs() 产出。
INPUTS_SPLIT = "inputs"

DEFAULT_SPLITS = ("train", "dev", "test")

# 重复单位受控词汇表（B9 + PILOT3 统计抽象回炉）：claim 的统计口径必须显式声明单位。
# train_seed：训练种子——bootstrap 只反映训练随机性，永远只是描述性。
# independent_run：独立重复试验（系统测量/数值重复等）——同样只描述重复间稳定性。
# analysis_unit：数据内的分析单元（区块/群组/时间块）——对独立单元做 bootstrap 是合法的
# 单元层抽样不确定性程序，满足预注册判据（单元数与方向比例）时允许 statistical_claim=True。
REPEAT_TRAIN_SEED = "train_seed"
REPEAT_INDEPENDENT_RUN = "independent_run"
REPEAT_ANALYSIS_UNIT = "analysis_unit"
REPEAT_UNITS = (REPEAT_TRAIN_SEED, REPEAT_INDEPENDENT_RUN, REPEAT_ANALYSIS_UNIT)


class UnsupportedEvaluator(LookupError):
    """未注册的评估器（未知领域包 / 未知指标）。

    注册表只抛这个；``popper.core`` 在边界把它转成 ``ProtocolError``，使调用方
    能用统一异常类型捕获。存在的意义是：未知契约必须「立即失败」，而不是落到
    某个 ``else`` 分支被当成二分类语义继续跑。
    """


# 引擎能写入工作区的输入制品角色；「候选怎么被调用」由这些角色名而不是
# 具体 CLI 参数名表达，避免核心流程内嵌某一种形状的字面量。
INVOCATION_INPUT_ROLES = ("train", "inputs", "config")
# 全部角色：prediction 是候选写出的输出制品，seed 是重复单位的取值。
INVOCATION_ROLES = (*INVOCATION_INPUT_ROLES, "prediction", "seed")

# 执行轨迹的保留制品名：执行端用它记录「候选确实跑过」的事实，候选不得占用，
# 否则可以写出伪造的执行证据。
EXECUTION_TRACE_NAME = "_popper_execution.json"


def _check_basename(name, label):
    """制品名必须是工作区内的纯文件名：拒绝空值、绝对路径、目录分隔与 '..' 片段。"""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{label} 必须是非空字符串: {name!r}")
    if os.path.isabs(name):
        raise ValueError(f"{label} 不能是绝对路径: {name!r}")
    if "/" in name or "\\" in name:
        raise ValueError(f"{label} 必须是纯文件名（不含目录分隔符）: {name!r}")
    if ".." in name.replace("\\", "/").split("/"):
        raise ValueError(f"{label} 不能包含 '..' 片段: {name!r}")


@dataclass(frozen=True)
class Invocation:
    """候选的调用契约：参数顺序 + 输入制品文件名 + 输出制品文件名模板。

    - args: ((flag, role), ...)，顺序即命令行顺序；role ∈ INVOCATION_ROLES
    - inputs: ((role, basename), ...)，role ∈ INVOCATION_INPUT_ROLES，basename 是纯文件名
    - prediction: 输出制品**纯文件名**模板，必须含 {seed}

    这里只声明「文件名」，不声明目录前缀：core 侧按 run_dir 布局解析，
    worker 侧按 inputs//outputs/ 布局解析，目录约定由执行端负责。
    """

    args: tuple
    inputs: tuple
    prediction: str

    def __post_init__(self):
        if not isinstance(self.args, tuple) or not self.args:
            raise ValueError(f"Invocation.args 必须是非空 tuple: {self.args!r}")
        flags, roles = set(), []
        for item in self.args:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError(f"Invocation.args 的每项必须是 (flag, role) 二元组: {item!r}")
            flag, role = item
            if not isinstance(flag, str) or not isinstance(role, str):
                raise ValueError(f"Invocation.args 的 flag 与 role 必须是字符串: {item!r}")
            if not flag.startswith("--"):
                raise ValueError(f"Invocation.args 的 flag 必须以 '--' 开头: {flag!r}")
            if flag in flags:
                raise ValueError(f"Invocation.args 的 flag 重复: {flag!r}")
            if role not in INVOCATION_ROLES:
                raise ValueError(f"Invocation.args 的 role 不是已声明角色: {role!r}")
            flags.add(flag)
            roles.append(role)
        for role in ("prediction", "seed"):
            if roles.count(role) != 1:
                raise ValueError(
                    f"Invocation.args 中 {role!r} 必须恰好出现一次（实际 {roles.count(role)} 次）")
        if not isinstance(self.inputs, tuple):
            raise ValueError(f"Invocation.inputs 必须是 tuple: {self.inputs!r}")
        seen = set()
        for item in self.inputs:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError(f"Invocation.inputs 的每项必须是 (role, basename) 二元组: {item!r}")
            role, basename = item
            if not isinstance(role, str) or role not in INVOCATION_INPUT_ROLES:
                raise ValueError(f"Invocation.inputs 的 role 不是已声明输入角色: {role!r}")
            if role in seen:
                raise ValueError(f"Invocation.inputs 的 role 重复: {role!r}")
            _check_basename(basename, f"Invocation.inputs 中 {role} 的 basename")
            seen.add(role)
        _check_basename(self.prediction, "Invocation.prediction")
        # 保留名先于占位符校验：它本身不含 {seed}，若后判就永远报不出真正的原因。
        if self.prediction == EXECUTION_TRACE_NAME:
            raise ValueError(f"Invocation.prediction 不能占用执行轨迹保留名: {self.prediction!r}")
        if "{seed}" not in self.prediction:
            raise ValueError(f"Invocation.prediction 必须含 '{{seed}}' 占位符: {self.prediction!r}")

    def input_basenames(self) -> dict:
        """{role: basename}：执行端按角色取输入制品文件名。"""
        return dict(self.inputs)

    def prediction_name(self, seed) -> str:
        """输出制品的纯文件名（seed 代入模板）。"""
        return self.prediction.format(seed=seed)

    def render(self, values) -> tuple:
        """按 args 声明的顺序展开为 argv。

        缺角色取值立即失败：未知契约必须显式报错，而不是猜一个默认值继续跑。
        """
        argv = []
        for flag, role in self.args:
            if role not in values:
                raise UnsupportedEvaluator(f"调用契约缺少角色取值: {role!r}（参数 {flag!r}）")
            argv.extend((flag, str(values[role])))
        return tuple(argv)


@dataclass(frozen=True)
class MetricSpec:
    """指标契约。把值域变成契约的一部分，新指标不必再改校验代码。

    ``value_domain`` 是闭区间 ``(下界, 上界)``，``None`` 表示该侧不设限。
    """

    name: str
    direction: Literal["min", "max"]
    unit: str = ""
    description: str = ""
    value_domain: tuple = (None, None)
    role: Literal["primary", "guardrail"] = "primary"
    min_improvement: float = 0.0

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("MetricSpec.name 必须是非空字符串")
        if self.direction not in ("min", "max"):
            raise ValueError(f"MetricSpec.direction 必须为 min/max: {self.direction!r}")
        if self.role not in ("primary", "guardrail"):
            raise ValueError(f"MetricSpec.role 必须为 primary/guardrail: {self.role!r}")
        if not isinstance(self.value_domain, tuple) or len(self.value_domain) != 2:
            raise ValueError("MetricSpec.value_domain 必须是 (下界, 上界) 二元组")
        low, high = self.value_domain
        if low is not None and high is not None and low > high:
            raise ValueError("MetricSpec.value_domain 的下界不能大于上界")

    def accepts(self, value) -> bool:
        """判定值是否落在声明值域内。"""
        low, high = self.value_domain
        return not ((low is not None and value < low) or (high is not None and value > high))

    def range_text(self) -> str:
        low, high = self.value_domain
        if low is None and high is None:
            return "任意有限值"
        if high is None:
            return f"≥ {low:g}"
        if low is None:
            return f"≤ {high:g}"
        return f"{low:g} ≤ 值 ≤ {high:g}"


class MetricCarrier:
    """``metrics()`` / ``metric()`` / ``primary_metric()`` 的共用实现。

    各形状基类（aligned / staged）都实现同一套指标访问，抽到这里避免抄成多份。
    类体内同名的方法会遮蔽模块级函数，因此此处一律显式调用模块级实现。
    """

    _metrics: tuple = ()

    def metrics(self) -> tuple:
        return self._metrics

    def metric(self, metric_id):
        """metric_id 为 None 时取 primary。"""
        return _primary(self.metrics()) if metric_id is None else metric_spec(self, metric_id)

    def primary_metric(self):
        return _primary(self.metrics())


@runtime_checkable
class DomainPack(Protocol):
    """一个领域包 = 一类科研任务的完整接入契约。

    实现者必须是模块级单例，并由 ``popper.domains`` 在导入期注册。注册期会校验
    形状、指标与冻结身份记录，使「加进注册表就静默改变语义」不可能发生。
    """

    pack_id: str
    schema_version: str
    evaluator_id: str
    task_shape: str

    def evaluator_entry(self) -> dict:
        """冻结的评估器身份记录：id / metric / definition / dataset。

        该记录被 ``evaluator_hash`` 绑定，语义变更必须换新 ID，否则旧实验会静默失效。
        """

    def invocation(self) -> "Invocation":
        """候选如何被调用：由形状基类提供。"""

    def metrics(self) -> tuple:
        """本域包的指标契约；恰好一个 primary，其余为 guardrail。"""

    def validate_rows(self, rows, split) -> None:
        """校验数据行。``split`` 为 splits() 中的一项，或保留的 INPUTS_SPLIT。"""

    def project_inputs(self, rows) -> list:
        """去掉标签，只留候选可见的输入。替代按评估器分派的 model_inputs。"""

    def row_identity(self, row):
        """去重与泄漏检查用的样本身份。替代无守卫的 sample_signature。"""

    def validate_predictions(self, rows, predictions, metric_id) -> None:
        """校验预测与评估数据的对齐关系。"""

    def score(self, rows, predictions, metric_id) -> float:
        """按 metric_id 计分。"""

    def splits(self) -> tuple:
        """本域包声明的数据划分。"""

    def repeat_unit(self) -> str:
        """重复单位，取值必须属于 ``REPEAT_UNITS``（注册期校验）。"""

    def unit_values(self, rows):
        """数据内分析单元的取值（如区块 id 的升序元组）；非分析单元域包返回 ``None``。

        返回 ``None`` 时重复取值来自 ``experiment.json`` 的 ``seeds``；返回非 ``None``
        时该域包 ``units_from_data=True``，``seeds`` 必须缺省，每个划分的重复取值
        由该划分的数据导出——训练种子/独立重复在各划分间共用同一组取值，而分析单元
        （区块/群组/时间块）随划分不同而不同。
        """

    def validate_splits(self, sets) -> None:
        """跨划分一致性（维度、类别集……）。默认无约束。"""


_PACKS: dict = {}


def _primary(specs):
    return next(spec for spec in specs if spec.role == "primary")


def register(pack):
    """注册域包，并在导入期做完所有可静态检查的校验。"""
    if not isinstance(pack.pack_id, str) or not pack.pack_id:
        raise ValueError("域包必须有非空 pack_id")
    if not isinstance(pack.evaluator_id, str) or not pack.evaluator_id:
        raise ValueError(f"域包 {pack.pack_id} 必须有非空 evaluator_id")
    if getattr(pack, "task_shape", None) not in IMPLEMENTED_SHAPES:
        raise ValueError(
            f"域包 {pack.pack_id} 声明了未实现的任务形状: {getattr(pack, 'task_shape', None)!r}")
    if pack.evaluator_id in _PACKS:
        raise ValueError(f"评估器 ID 重复注册: {pack.evaluator_id}")
    entry = pack.evaluator_entry()
    if set(entry) != {"id", "metric", "definition", "dataset"}:
        raise ValueError(f"域包 {pack.pack_id} 的 evaluator_entry 不符合冻结格式")
    if entry["id"] != pack.evaluator_id:
        raise ValueError(f"域包 {pack.pack_id} 的 evaluator_entry.id 与 evaluator_id 不一致")
    specs = pack.metrics()
    if not specs or any(not isinstance(spec, MetricSpec) for spec in specs):
        raise ValueError(f"域包 {pack.pack_id} 必须声明至少一个 MetricSpec")
    if len({spec.name for spec in specs}) != len(specs):
        raise ValueError(f"域包 {pack.pack_id} 的指标名重复")
    primaries = [spec for spec in specs if spec.role == "primary"]
    if len(primaries) != 1:
        raise ValueError(f"域包 {pack.pack_id} 必须恰好声明一个 primary 指标")
    frozen = entry["metric"]
    if (frozen.get("name") != primaries[0].name
            or frozen.get("direction") != primaries[0].direction):
        raise ValueError(f"域包 {pack.pack_id} 的 evaluator_entry.metric 与 primary 指标不一致")
    invocation = pack.invocation()
    if not isinstance(invocation, Invocation):
        raise ValueError(f"域包 {pack.pack_id} 的调用契约必须是 Invocation")
    if pack.repeat_unit() not in REPEAT_UNITS:
        raise ValueError(
            f"域包 {pack.pack_id} 的 repeat_unit 不属于受控词汇表 REPEAT_UNITS: "
            f"{pack.repeat_unit()!r}")
    _PACKS[pack.evaluator_id] = pack
    return pack


def get(evaluator_id):
    """按评估器 ID 取域包；未知契约立即抛 UnsupportedEvaluator。"""
    if evaluator_id not in _PACKS:
        raise UnsupportedEvaluator(f"未支持的评估器: {evaluator_id!r}（未注册的领域包）")
    return _PACKS[evaluator_id]


def metric_spec(pack, metric_id):
    """按指标名取 MetricSpec；未声明即立即失败。"""
    for spec in pack.metrics():
        if spec.name == metric_id:
            return spec
    raise UnsupportedEvaluator(f"未支持的指标: {metric_id!r}（域包 {pack.pack_id} 未声明）")


def primary_metric(pack):
    return _primary(pack.metrics())


def packs():
    return tuple(_PACKS.values())


def registered_evaluators():
    """``popper.core.EVALUATORS`` 的数据来源：评估器 ID → 冻结身份记录。"""
    return {pack.evaluator_id: pack.evaluator_entry() for pack in _PACKS.values()}


def registered_metrics():
    """规范指标名 → MetricSpec，消除「第三套命名体系」。"""
    return {spec.name: spec for pack in _PACKS.values() for spec in pack.metrics()}
