"""staged_artifacts 形状与调用契约的契约测试（批次 D3 的出口条件）。

覆盖五件事：
1. 调用契约由域包声明：aligned 声明与改造前的字面量逐字节一致，静态非法的声明在注册期失败；
2. staged 形状：指标由域包从**原始测量**派生，候选自报指标/字段非法立即失败；
3. 接入成本：运行期注册第二个 staged 域包后，dataset/score/initialize/evaluator_for_metric 全部可用；
4. 测量字段可以是**正数值序列**（整条曲线交给域包），逐元素校验；
5. 两个试点领域（algorithm-throughput-v1 / convergence-order-v1）跑通
   init → search → freeze → confirm → replay，且 replay 不执行代码。
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper import core
from popper.core import (EVALUATORS, Experiment, ProtocolError, dataset, evaluator_for_metric, initialize,
                         model_inputs, score, write_json)
from popper.domains import protocol, register, registered_metrics
from popper.domains.protocol import EXECUTION_TRACE_NAME, Invocation, MetricSpec
from popper.domains.staged import StagedArtifactsPack
from popper.research.confirmation_runner import require_aligned_prediction


PILOT = Path(__file__).resolve().parents[1] / "examples" / "throughput-sort"
PILOT_FILES = ("experiment.json", "model.py", "train.json", "dev.json", "test.json")

PILOT_ODE = Path(__file__).resolve().parents[1] / "examples" / "ode-convergence"
PILOT_ODE_FILES = ("experiment.json", "model.py", "train.json", "dev.json", "test.json")

WORKLOAD = [{"id": "w1", "n": 40}, {"id": "w2", "n": 80}]
MEASUREMENT = {"operations": 128, "elapsed_seconds": 0.5}

SERIES_WORKLOAD = [{"id": "g1", "grid": 8}, {"id": "g2", "grid": 16}]
SERIES_MEASUREMENT = {"steps": [8, 16], "values": [0.5, 0.125]}

# 收敛研究：同一问题实例（rate/t_end 全表一致）、步数严格递增；误差随网格减半。
CONVERGENCE_ROWS = [{"id": "g1", "steps": 8, "rate": 1.0, "t_end": 1.0},
                    {"id": "g2", "steps": 16, "rate": 1.0, "t_end": 1.0},
                    {"id": "g3", "steps": 32, "rate": 1.0, "t_end": 1.0}]
CONVERGENCE_PAYLOAD = {"steps": [8, 16, 32], "errors": [1e-2, 5e-3, 2.5e-3]}


class _ThroughputProbePack(StagedArtifactsPack):
    """探针 staged 域包：只声明形状、调用契约与一个「操作数 / 秒」指标。"""

    pack_id = "staged-probe"
    evaluator_id = "staged-probe-v1"
    workload_fields = (("n", "int"),)
    measurement_fields = (("operations", "int"), ("elapsed_seconds", "number"))
    _metrics = (MetricSpec(name="probe_throughput", direction="max", unit="operations_per_second",
                           value_domain=(0, None)),)
    _entry = {"id": "staged-probe-v1", "metric": {"name": "probe_throughput", "direction": "max"},
              "definition": "probe operations per elapsed second", "dataset": "rows[id,n]"}

    def invocation(self):
        return Invocation(
            args=(("--workload", "inputs"), ("--output", "prediction"), ("--seed", "seed")),
            inputs=(("inputs", "workload.json"),),
            prediction="measurement-{seed}.json",
        )

    def score_measurements(self, measurements, metric_id):
        return measurements["operations"] / measurements["elapsed_seconds"]


class _OutOfDomainProbePack(_ThroughputProbePack):
    """计分结果落在声明值域之外：值域必须是契约的一部分，而不是注释。"""

    pack_id = "staged-probe-out-of-domain"
    evaluator_id = "staged-probe-out-of-domain-v1"
    _metrics = (MetricSpec(name="probe_bounded", direction="min", value_domain=(1, None)),)
    _entry = {"id": "staged-probe-out-of-domain-v1",
              "metric": {"name": "probe_bounded", "direction": "min"},
              "definition": "probe that reports outside its declared value domain",
              "dataset": "rows[id,n]"}

    def score_measurements(self, measurements, metric_id):
        return 0.5


class _NonFiniteProbePack(_ThroughputProbePack):
    """计分结果非有限值：必须立即失败，而不是写进证据。"""

    pack_id = "staged-probe-non-finite"
    evaluator_id = "staged-probe-non-finite-v1"
    _metrics = (MetricSpec(name="probe_infinite", direction="max"),)
    _entry = {"id": "staged-probe-non-finite-v1",
              "metric": {"name": "probe_infinite", "direction": "max"},
              "definition": "probe that reports a non-finite score", "dataset": "rows[id,n]"}

    def score_measurements(self, measurements, metric_id):
        return float("nan")


class _LatencyProbePack(StagedArtifactsPack):
    """第二个 staged 领域：不声明 config 角色，制品名与参数名都与前一个不同。"""

    pack_id = "queue-latency"
    evaluator_id = "queue-latency-v1"
    workload_fields = (("jobs", "int"),)
    measurement_fields = (("processed", "int"), ("elapsed_seconds", "number"))
    _metrics = (MetricSpec(name="latency", direction="min", unit="seconds_per_job",
                           value_domain=(0, None)),)
    _entry = {"id": "queue-latency-v1", "metric": {"name": "latency", "direction": "min"},
              "definition": "elapsed seconds per processed job", "dataset": "rows[id,jobs]"}

    def invocation(self):
        return Invocation(
            args=(("--queue", "inputs"), ("--output", "prediction"), ("--seed", "seed")),
            inputs=(("inputs", "queue.json"),),
            prediction="latency-{seed}.json",
        )

    def score_measurements(self, measurements, metric_id):
        return measurements["elapsed_seconds"] / measurements["processed"]


class _SeriesProbePack(StagedArtifactsPack):
    """探针 staged 域包：测量字段声明为「正数值序列」，覆盖序列取值校验。"""

    pack_id = "staged-probe-series"
    evaluator_id = "staged-probe-series-v1"
    workload_fields = (("grid", "int"),)
    measurement_fields = (("steps", "series"), ("values", "series"))
    _metrics = (MetricSpec(name="probe_min_value", direction="max", unit="value",
                           value_domain=(0, None)),)
    _entry = {"id": "staged-probe-series-v1",
              "metric": {"name": "probe_min_value", "direction": "max"},
              "definition": "probe whose measurement fields are positive number series",
              "dataset": "rows[id,grid]"}

    def invocation(self):
        return Invocation(
            args=(("--grid", "inputs"), ("--output", "prediction"), ("--seed", "seed")),
            inputs=(("inputs", "grid.json"),),
            prediction="series-{seed}.json",
        )

    def score_measurements(self, measurements, metric_id):
        return min(measurements["values"])


class _NotAnInvocationPack(_ThroughputProbePack):
    """调用契约必须是 Invocation：返回其它类型在注册期立即失败。"""

    pack_id = "staged-probe-no-invocation"
    evaluator_id = "staged-probe-no-invocation-v1"
    _metrics = (MetricSpec(name="probe_unbound", direction="max"),)
    _entry = {"id": "staged-probe-no-invocation-v1",
              "metric": {"name": "probe_unbound", "direction": "max"},
              "definition": "probe without an invocation contract", "dataset": "rows[id,n]"}

    def invocation(self):
        return None


class _RegisterMixin:
    """注册探针域包，并在用例结束后从全局注册表移除。"""

    def register(self, pack):
        register(pack)
        self.addCleanup(protocol._PACKS.pop, pack.evaluator_id, None)
        return pack


class InvocationContractTests(unittest.TestCase):
    def test_aligned_declaration_matches_the_frozen_literals(self):
        """逐样本契约必须与核心流程改造前的字面量逐字节一致，否则旧实验会失效。"""
        expected = Invocation(
            args=(("--train", "train"), ("--input", "inputs"), ("--output", "prediction"),
                  ("--config", "config"), ("--seed", "seed")),
            inputs=(("train", "train.json"), ("inputs", "inputs.json"), ("config", "config.json")),
            prediction="predictions-{seed}.json",
        )
        for evaluator_id in ("mse-v1", "binary-accuracy-v1", "mae-v1", "binary-f1-v1",
                             "multiclass-macro-f1-v1"):
            with self.subTest(evaluator_id=evaluator_id):
                self.assertEqual(expected, protocol.get(evaluator_id).invocation())
        self.assertEqual("predictions-11.json", expected.prediction_name(11))
        self.assertEqual({"train": "train.json", "inputs": "inputs.json",
                          "config": "config.json"}, expected.input_basenames())

    def test_role_render_refuses_unknown_roles(self):
        invocation = protocol.get("mse-v1").invocation()
        with self.assertRaises(protocol.UnsupportedEvaluator) as caught:
            invocation.render({"prediction": "out.json", "seed": "11"})
        self.assertIn("train", str(caught.exception))

    def test_statically_invalid_invocations_are_rejected(self):
        cases = {
            "args 为空": dict(args=(), inputs=(), prediction="p-{seed}.json"),
            "args 不是二元组": dict(args=(("--train",),), inputs=(), prediction="p-{seed}.json"),
            "flag 缺前缀": dict(args=(("train", "seed"), ("--out", "prediction")), inputs=(),
                              prediction="p-{seed}.json"),
            "flag 重复": dict(args=(("--out", "prediction"), ("--out", "seed")), inputs=(),
                            prediction="p-{seed}.json"),
            "未知角色": dict(args=(("--out", "prediction"), ("--s", "seed"), ("--t", "target")),
                          inputs=(), prediction="p-{seed}.json"),
            "prediction 缺失": dict(args=(("--s", "seed"),), inputs=(), prediction="p-{seed}.json"),
            "seed 重复": dict(args=(("--s", "seed"), ("--s2", "seed"), ("--out", "prediction")),
                            inputs=(), prediction="p-{seed}.json"),
            "输入角色非法": dict(args=(("--out", "prediction"), ("--s", "seed")),
                            inputs=(("prediction", "p.json"),), prediction="p-{seed}.json"),
            "输入角色重复": dict(args=(("--out", "prediction"), ("--s", "seed")),
                            inputs=(("inputs", "a.json"), ("inputs", "b.json")),
                            prediction="p-{seed}.json"),
            "输入名是绝对路径": dict(args=(("--out", "prediction"), ("--s", "seed")),
                              inputs=(("inputs", "/tmp/a.json"),), prediction="p-{seed}.json"),
            "输入名含目录分隔": dict(args=(("--out", "prediction"), ("--s", "seed")),
                              inputs=(("inputs", "sub/a.json"),), prediction="p-{seed}.json"),
            "输入名含上跳": dict(args=(("--out", "prediction"), ("--s", "seed")),
                             inputs=(("inputs", "../a.json"),), prediction="p-{seed}.json"),
            "制品名是绝对路径": dict(args=(("--out", "prediction"), ("--s", "seed")), inputs=(),
                              prediction="/tmp/p-{seed}.json"),
            "制品名占用保留路径": dict(args=(("--out", "prediction"), ("--s", "seed")), inputs=(),
                               prediction=EXECUTION_TRACE_NAME),
            "制品名缺占位符": dict(args=(("--out", "prediction"), ("--s", "seed")), inputs=(),
                             prediction="predictions.json"),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    Invocation(**kwargs)

    def test_pack_without_a_real_invocation_cannot_register(self):
        with self.assertRaises(ValueError) as caught:
            register(_NotAnInvocationPack())
        self.assertIn("调用契约必须是 Invocation", str(caught.exception))
        self.assertNotIn("staged-probe-no-invocation-v1", EVALUATORS)


class StagedShapeTests(_RegisterMixin, unittest.TestCase):
    def setUp(self):
        self.pack = self.register(_ThroughputProbePack())

    def test_metric_is_derived_from_raw_measurements(self):
        self.assertEqual(256.0, score(WORKLOAD, MEASUREMENT, "staged-probe-v1"))
        self.assertEqual(256.0, self.pack.score(WORKLOAD, MEASUREMENT, None))
        self.assertEqual("independent_run", self.pack.repeat_unit())
        self.assertEqual("staged_artifacts", self.pack.task_shape)

    def test_self_reported_metric_is_rejected(self):
        payload = {**MEASUREMENT, "probe_throughput": 999.0}
        with self.assertRaises(ProtocolError) as caught:
            score(WORKLOAD, payload, "staged-probe-v1")
        self.assertIn("不得自行声明指标", str(caught.exception))

    def test_artifact_fields_must_match_the_declaration(self):
        for label, payload, expected in (
                ("缺字段", {"operations": 8}, "缺少声明字段: elapsed_seconds"),
                ("多字段", {**MEASUREMENT, "extra": 1}, "含未声明字段: extra"),
                ("非对象", [1, 2, 3], "制品必须是 JSON 对象")):
            with self.subTest(case=label):
                with self.assertRaises(ProtocolError) as caught:
                    score(WORKLOAD, payload, "staged-probe-v1")
                self.assertIn(expected, str(caught.exception))

    def test_artifact_values_must_be_positive_and_typed(self):
        for label, payload, field in (
                ("操作数为零", {**MEASUREMENT, "operations": 0}, "operations"),
                ("操作数是布尔", {**MEASUREMENT, "operations": True}, "operations"),
                ("操作数是小数", {**MEASUREMENT, "operations": 1.5}, "operations"),
                ("耗时为字符串", {**MEASUREMENT, "elapsed_seconds": "0.5"}, "elapsed_seconds"),
                ("耗时为负", {**MEASUREMENT, "elapsed_seconds": -1.0}, "elapsed_seconds")):
            with self.subTest(case=label):
                with self.assertRaises(ProtocolError) as caught:
                    score(WORKLOAD, payload, "staged-probe-v1")
                self.assertIn(f"字段 {field}", str(caught.exception))

    def test_workload_rows_must_match_the_declaration(self):
        for label, rows, expected in (
                ("空清单", [], "非空列表"),
                ("行非对象", ["w1"], "必须是 JSON 对象"),
                ("缺字段", [{"id": "w1"}], "缺少声明字段: n"),
                ("多字段", [{"id": "w1", "n": 1, "k": 2}], "含未声明字段: k"),
                ("规模为零", [{"id": "w1", "n": 0}], "字段 n 必须是正的整数值"),
                ("id 重复", [{"id": "w1", "n": 1}, {"id": "w1", "n": 2}], "唯一")):
            with self.subTest(case=label):
                with self.assertRaises(ProtocolError) as caught:
                    self.pack.validate_rows(rows, None)
                self.assertIn(expected, str(caught.exception))

    def test_undeclared_metric_fails_immediately(self):
        with self.assertRaises(ProtocolError) as caught:
            core.evaluator_metric("staged-probe-v1", "probe_mse")
        self.assertIn("未支持的指标", str(caught.exception))
        with self.assertRaises(protocol.UnsupportedEvaluator):
            self.pack.metric("probe_mse")

    def test_workload_list_is_the_candidate_visible_input(self):
        self.assertEqual(WORKLOAD, model_inputs(WORKLOAD, "staged-probe-v1"))
        self.assertNotEqual(core.sample_signature(WORKLOAD[0], "staged-probe-v1"),
                            core.sample_signature(WORKLOAD[1], "staged-probe-v1"))

    def test_score_outside_declared_domain_is_rejected(self):
        self.register(_OutOfDomainProbePack())
        with self.assertRaises(ProtocolError) as caught:
            score(WORKLOAD, MEASUREMENT, "staged-probe-out-of-domain-v1")
        self.assertIn("超出声明值域", str(caught.exception))
        self.assertIn("≥ 1", str(caught.exception))

    def test_non_finite_score_is_rejected(self):
        self.register(_NonFiniteProbePack())
        with self.assertRaises(ProtocolError) as caught:
            score(WORKLOAD, MEASUREMENT, "staged-probe-non-finite-v1")
        self.assertIn("非有限值", str(caught.exception))


class SecondStagedPackTests(_RegisterMixin, unittest.TestCase):
    """第二个 staged 领域：一个新文件 + 一行注册即可，判定层与主流程不改。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.pack = self.register(_LatencyProbePack())

    def tearDown(self):
        self.temp.cleanup()

    def write_project(self):
        write_json(self.project / "queue.json", [{"id": "q1", "jobs": 100}, {"id": "q2", "jobs": 200}])
        write_json(self.project / "train.json", [{"id": "t1", "jobs": 50}])
        write_json(self.project / "test.json", [{"id": "e1", "jobs": 300}])
        (self.project / "worker.py").write_text("print('placeholder')\n", encoding="utf-8")
        write_json(self.project / "experiment.json", {
            "name": "队列延迟演示", "objective": "演示第二个 staged 领域的接入成本",
            "entrypoint": "worker.py", "code_files": ["worker.py"],
            "train": "train.json", "dev": "queue.json", "test": "test.json",
            "baseline": {"strategy": "fifo"}, "candidates": [{"strategy": "sjf"}],
            "metric": {"name": "latency", "direction": "min"},
            "seeds": [11], "budget": 1, "timeout_seconds": 30, "min_improvement": 0.0})

    def test_entrypoints_are_available_without_touching_the_judging_layer(self):
        self.write_project()
        rows = dataset(str(self.project / "queue.json"), "queue-latency-v1")
        self.assertEqual(0.02, score(rows, {"processed": 100, "elapsed_seconds": 2.0},
                                     "queue-latency-v1"))
        self.assertEqual("queue-latency-v1",
                         evaluator_for_metric({"name": "latency", "direction": "min"}))
        self.assertEqual("latency", core.evaluator_metric("queue-latency-v1").name)
        self.assertEqual("min", self.pack.primary_metric().direction)
        state = initialize(self.project)
        self.assertEqual("queue-latency-v1", state["evaluator_id"])
        # 输入落位名与制品牌名同样来自声明：这里没有 config 角色，也没有 inputs.json。
        self.assertEqual((("inputs", "queue.json"),), self.pack.invocation().inputs)
        self.assertEqual("latency-11.json", self.pack.invocation().prediction_name(11))


class SeriesMeasurementTests(_RegisterMixin, unittest.TestCase):
    """测量字段可以是「正数值序列」：整条曲线交给域包，而不是先压成一个标量。"""

    def setUp(self):
        self.pack = self.register(_SeriesProbePack())

    def test_a_positive_series_is_accepted_element_wise(self):
        self.assertEqual(0.125, score(SERIES_WORKLOAD, SERIES_MEASUREMENT, "staged-probe-series-v1"))
        self.assertEqual(0.125, self.pack.score(SERIES_WORKLOAD, SERIES_MEASUREMENT, None))

    def test_series_must_be_a_non_empty_list(self):
        for label, value in (("空序列", []), ("不是列表", 8), ("是字符串", "8,16")):
            with self.subTest(case=label):
                with self.assertRaises(ProtocolError) as caught:
                    score(SERIES_WORKLOAD, {**SERIES_MEASUREMENT, "steps": value},
                          "staged-probe-series-v1")
                self.assertIn("字段 steps 必须是非空的数值序列", str(caught.exception))

    def test_every_series_element_must_be_a_positive_finite_number(self):
        for label, item in (("含零", 0), ("含负数", -1.0), ("含非有限值", float("inf")),
                            ("含 NaN", float("nan")), ("含字符串", "0.5"), ("含布尔", True)):
            with self.subTest(case=label):
                with self.assertRaises(ProtocolError) as caught:
                    score(SERIES_WORKLOAD, {**SERIES_MEASUREMENT, "values": [0.5, item]},
                          "staged-probe-series-v1")
                self.assertIn("字段 values 的数值序列含非正有限数值", str(caught.exception))


class ConvergenceOrderContractTests(unittest.TestCase):
    """数值收敛阶域包：细化表与误差序列都必须能支撑一次真实的阶数拟合。"""

    def setUp(self):
        self.pack = protocol.get("convergence-order-v1")

    def test_order_is_fitted_from_the_error_series(self):
        self.assertEqual("convergence_order", self.pack.primary_metric().name)
        self.assertEqual("max", self.pack.primary_metric().direction)
        self.assertEqual("independent_run", self.pack.repeat_unit())
        self.assertEqual("convergence-order-v1",
                         evaluator_for_metric({"name": "convergence_order", "direction": "max"}))
        # 误差随网格减半 ⇒ log-log 斜率 -1 ⇒ 观测阶数 1。
        self.assertAlmostEqual(1.0, score(CONVERGENCE_ROWS, CONVERGENCE_PAYLOAD,
                                          "convergence-order-v1"), places=12)

    def test_declaration_has_no_training_stage_and_names_its_own_artifacts(self):
        invocation = self.pack.invocation()
        self.assertEqual({"inputs": "convergence.json", "config": "scheme.json"},
                         invocation.input_basenames())
        self.assertEqual("measurements-11.json", invocation.prediction_name(11))
        self.assertNotIn("train", invocation.input_basenames())
        self.assertIn("convergence_order", registered_metrics())

    def test_schedule_must_support_a_fit(self):
        for label, rows, expected in (
                ("单网格", CONVERGENCE_ROWS[:1], "至少需要两个网格"),
                ("步数非严格递增",
                 [{"id": "g1", "steps": 8, "rate": 1.0, "t_end": 1.0},
                  {"id": "g2", "steps": 8, "rate": 1.0, "t_end": 1.0},
                  {"id": "g3", "steps": 32, "rate": 1.0, "t_end": 1.0}], "严格递增"),
                ("多个问题实例",
                 [{"id": "g1", "steps": 8, "rate": 1.0, "t_end": 1.0},
                  {"id": "g2", "steps": 16, "rate": 1.0, "t_end": 1.0},
                  {"id": "g3", "steps": 32, "rate": 2.0, "t_end": 1.0}], "只针对一个问题实例")):
            with self.subTest(case=label):
                with self.assertRaises(ProtocolError) as caught:
                    self.pack.validate_rows(rows, "dev")
                self.assertIn(expected, str(caught.exception))

    def test_artifact_must_restate_the_declared_schedule(self):
        for label, payload, expected in (
                ("复述的步数不同", {"steps": [8, 16, 64], "errors": [1e-2, 5e-3, 2.5e-3]},
                 "复述的步数序列与声明的细化表不一致"),
                ("误差个数不匹配", {"steps": [8, 16, 32], "errors": [1e-2, 5e-3]},
                 "逐网格一一对应")):
            with self.subTest(case=label):
                with self.assertRaises(ProtocolError) as caught:
                    score(CONVERGENCE_ROWS, payload, "convergence-order-v1")
                self.assertIn(expected, str(caught.exception))

    def test_error_series_must_decay_with_refinement(self):
        for label, errors in (("误差不降", [1e-2, 1e-2, 2.5e-3]),
                              ("误差回升", [1e-2, 8e-3, 9e-3])):
            with self.subTest(case=label):
                with self.assertRaises(ProtocolError) as caught:
                    score(CONVERGENCE_ROWS, {"steps": [8, 16, 32], "errors": errors},
                          "convergence-order-v1")
                self.assertIn("严格下降", str(caught.exception))

    def test_self_reported_order_is_rejected(self):
        with self.assertRaises(ProtocolError) as caught:
            score(CONVERGENCE_ROWS, {**CONVERGENCE_PAYLOAD, "convergence_order": 4.0},
                  "convergence-order-v1")
        self.assertIn("不得自行声明指标", str(caught.exception))


class PilotThroughputEndToEndTests(unittest.TestCase):
    """试点领域 algorithm-throughput-v1：五个阶段跑通，且 replay 不执行候选代码。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in PILOT_FILES:
            shutil.copyfile(PILOT / name, self.root / name)
        self.exp = None

    def tearDown(self):
        if self.exp:
            self.exp.close()
        self.temp.cleanup()

    def start(self):
        initialize(self.root)
        self.exp = Experiment(self.root)
        return self.exp

    def test_five_phases_with_declared_artifacts_and_offline_replay(self):
        exp = self.start()
        self.assertEqual("algorithm-throughput-v1", exp.state()["evaluator_id"])
        self.assertEqual(len(exp.state()["spec"]["candidates"]) + 1, len(exp.search(True)))
        self.assertEqual("searching", exp.state()["phase"])
        self.assertEqual([], exp.results("test"))
        exp.freeze()
        claim = exp.confirm(True)
        self.assertIn(claim["status"], {"supports_threshold", "insufficient_evidence"})
        self.assertIs(claim["stats"]["statistical_claim"], False)
        self.assertEqual("independent_run", claim["stats"]["repeat_unit"])
        self.assertEqual("absolute_throughput_increase", claim["unit"])
        for result in exp.results():
            run_dir = exp.home / "runs" / result["run_id"]
            seed = result["per_seed"][0]["seed"]
            # 制品牌名与输入落位名都来自域包声明，核心流程不含这些字面量。
            self.assertTrue((run_dir / f"measurements-{seed}.json").is_file())
            self.assertTrue((run_dir / "warmup.json").is_file())
            self.assertTrue((run_dir / "workload.json").is_file())
            self.assertTrue((run_dir / "algorithm.json").is_file())
            self.assertFalse((run_dir / f"predictions-{seed}.json").exists())
            self.assertFalse((run_dir / "inputs.json").exists())
        with patch("popper.sandbox.launch",
                   side_effect=AssertionError("replay must not execute candidate code")):
            replay = exp.replay()
        self.assertEqual(len(exp.results()), replay["runs_recomputed"])
        self.assertTrue(replay["claim_recomputed"])
        self.assertTrue(Path(exp.report()).is_file())

    def test_staged_pilot_cannot_enter_holdout(self):
        with self.assertRaises(ProtocolError) as caught:
            require_aligned_prediction("algorithm-throughput-v1")
        self.assertIn("未支持的任务形状", str(caught.exception))
        self.assertIn("staged_artifacts", str(caught.exception))

    def test_pilot_declaration_uses_non_default_names(self):
        pack = protocol.get("algorithm-throughput-v1")
        self.assertEqual({"train": "warmup.json", "inputs": "workload.json",
                          "config": "algorithm.json"}, pack.invocation().input_basenames())
        self.assertEqual("measurements-11.json", pack.invocation().prediction_name(11))
        self.assertEqual("independent_run", pack.repeat_unit())
        self.assertIn("throughput", registered_metrics())


class PilotConvergenceEndToEndTests(unittest.TestCase):
    """试点领域 convergence-order-v1：序列测量的五个阶段跑通，replay 不执行候选代码。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in PILOT_ODE_FILES:
            shutil.copyfile(PILOT_ODE / name, self.root / name)
        self.exp = None

    def tearDown(self):
        if self.exp:
            self.exp.close()
        self.temp.cleanup()

    def start(self):
        initialize(self.root)
        self.exp = Experiment(self.root)
        return self.exp

    def test_five_phases_with_series_measurements_and_offline_replay(self):
        exp = self.start()
        self.assertEqual("convergence-order-v1", exp.state()["evaluator_id"])
        self.assertEqual(len(exp.state()["spec"]["candidates"]) + 1, len(exp.search(True)))
        self.assertEqual("searching", exp.state()["phase"])
        self.assertEqual([], exp.results("test"))
        exp.freeze()
        claim = exp.confirm(True)
        self.assertIn(claim["status"], {"supports_threshold", "insufficient_evidence"})
        self.assertIs(claim["stats"]["statistical_claim"], False)
        self.assertEqual("independent_run", claim["stats"]["repeat_unit"])
        self.assertEqual("absolute_convergence_order_increase", claim["unit"])
        for result in exp.results():
            run_dir = exp.home / "runs" / result["run_id"]
            seed = result["per_seed"][0]["seed"]
            # 输入落位名与测量制品牌名都来自域包声明。
            self.assertTrue((run_dir / "convergence.json").is_file())
            self.assertTrue((run_dir / "scheme.json").is_file())
            self.assertTrue((run_dir / f"measurements-{seed}.json").is_file())
            # 本领域不声明 train 角色，也不走逐样本形状。
            self.assertFalse((run_dir / "train.json").exists())
            self.assertFalse((run_dir / "warmup.json").exists())
            self.assertFalse((run_dir / "inputs.json").exists())
            self.assertFalse((run_dir / f"predictions-{seed}.json").exists())
        with patch("popper.sandbox.launch",
                   side_effect=AssertionError("replay must not execute candidate code")):
            replay = exp.replay()
        self.assertEqual(len(exp.results()), replay["runs_recomputed"])
        self.assertTrue(replay["claim_recomputed"])
        self.assertTrue(Path(exp.report()).is_file())

    def test_fitted_order_separates_the_schemes(self):
        exp = self.start()
        exp.search(True)
        exp.freeze()
        claim = exp.confirm(True)
        dev = {result["config"]["scheme"]: result["mean"] for result in exp.results("dev")}
        test = {result["config"]["scheme"]: result["mean"] for result in exp.results("test")}
        # 显式单步法的理论阶数：euler 1 / midpoint 2 / rk4 4，观测值应落在邻域内。
        self.assertAlmostEqual(1.0, dev["euler"], delta=0.1)
        self.assertAlmostEqual(2.0, dev["midpoint"], delta=0.1)
        self.assertAlmostEqual(4.0, dev["rk4"], delta=0.1)
        self.assertEqual("supports_threshold", claim["status"])
        # claim 的差值取自最终测试划分，冻结后的选择也不得改变这一口径。
        self.assertAlmostEqual(test["rk4"] - test["euler"], claim["delta"], places=9)

    def test_convergence_pilot_cannot_enter_holdout(self):
        with self.assertRaises(ProtocolError) as caught:
            require_aligned_prediction("convergence-order-v1")
        self.assertIn("staged_artifacts", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
