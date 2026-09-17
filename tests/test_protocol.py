import json
import runpy
import shutil
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from popper.core import EVALUATORS, Experiment, ProtocolError, digest, initialize, read_json, score, write_json
from popper.cli import main
from popper.proposer import make_proposer
from popper.server import Workstation
from popper import server as popper_server


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.root / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.root)
        self.exp = None

    def tearDown(self):
        if self.exp:
            self.exp.close()
        self.temp.cleanup()

    def start(self):
        initialize(self.root)
        self.exp = Experiment(self.root)
        return self.exp

    def test_real_loop_and_offline_replay(self):
        exp = self.start()
        results = exp.search(True)
        self.assertEqual(4, len(results))
        self.assertEqual("searching", exp.state()["phase"])
        self.assertEqual([], exp.results("test"))
        exp.freeze()
        claim = exp.confirm(True)
        self.assertEqual("supports_threshold", claim["status"])
        self.assertGreater(claim["delta"], 0.1)
        self.assertIn("stats", claim)
        self.assertIn("p_direction_consistent", claim["stats"])
        self.assertIn("descriptive", claim["stats"])
        self.assertIs(claim["stats"]["statistical_claim"], False)
        self.assertEqual("train_seed", claim["stats"]["repeat_unit"])
        with patch("popper.sandbox.launch", side_effect=AssertionError("replay must not execute")):
            replay = exp.replay()
        self.assertEqual(6, replay["runs_recomputed"])
        self.assertTrue(replay["claim_recomputed"])
        self.assertIn("decisions_replayed", replay)
        self.assertEqual(len(exp.results("dev")) - 1, len(replay["decisions_replayed"]))
        self.assertTrue(all(d["source"] in {"registered_queue", "byok"} for d in replay["decisions_replayed"]))
        self.assertTrue(Path(exp.report()).is_file())
        # Reopening a completed project preserves state and evidence.
        exp.close()
        self.exp = Experiment(self.root)
        self.assertEqual(claim, self.exp.state()["claim"])

    def test_evaluator_contract_is_versioned_independently(self):
        exp = self.start()
        self.assertEqual("mse-v1", exp.state()["evaluator_id"])
        self.assertEqual(digest(EVALUATORS["mse-v1"]), exp.state()["evaluator_hash"])
        result = exp.evaluate({"degree": 1}, "dev", True)
        self.assertEqual("mse-v1", result["evaluator_id"])

    def test_final_test_cannot_be_reused_or_search_resumed(self):
        exp = self.start()
        with self.assertRaises(ProtocolError):
            exp.confirm(True)
        with self.assertRaises(ProtocolError):
            exp.evaluate({"degree": 1}, "test", True)
        exp.search(True)
        exp.freeze()
        with self.assertRaises(ProtocolError):
            exp.search(True)
        exp.confirm(True)
        with self.assertRaises(ProtocolError):
            exp.confirm(True)

    def test_adjudicate_records_verdict_into_event_chain(self):
        exp = self.start()
        before = exp.state()
        self.assertNotIn("adjudications", before)
        with self.assertRaises(ProtocolError):
            exp.adjudicate("R6", "allow", "   ")
        with self.assertRaises(ProtocolError):
            exp.adjudicate("R9", "allow", "理由")
        with self.assertRaises(ProtocolError):
            exp.adjudicate("R6", "maybe", "理由")
        res = exp.adjudicate("R6", "allow", "held-out 在该用例域不适用，裁定放行")
        self.assertEqual("adjudicated", res["status"])
        state = exp.state()
        self.assertEqual("allow", state["adjudications"]["R6"]["verdict"])
        # 事件溯源链记录裁定，可重放/离线校验。
        row = exp.db.execute(
            "SELECT payload FROM events WHERE kind='gate_adjudicated' ORDER BY seq DESC LIMIT 1").fetchone()
        payload = json.loads(row["payload"])
        self.assertEqual("R6", payload["risk_id"])
        self.assertEqual("allow", payload["verdict"])
        # 重复裁定覆盖并再入链。
        exp.adjudicate("R6", "reject", "重新评估后驳回")
        self.assertEqual("reject", exp.state()["adjudications"]["R6"]["verdict"])

    def test_test_failure_consumes_access(self):
        exp = self.start()
        exp.search(True)
        exp.freeze()
        with patch("popper.sandbox.launch", side_effect=OSError("injected execution failure")):
            with self.assertRaises(ProtocolError):
                exp.confirm(True)
        self.assertEqual("confirmation_failed", exp.state()["phase"])
        with self.assertRaises(ProtocolError):
            exp.confirm(True)
        self.assertEqual(1, exp.db.execute("SELECT COUNT(*) FROM events WHERE kind='test_consumed'").fetchone()[0])

    def test_modified_code_and_config_are_rejected(self):
        exp = self.start()
        (self.root / "model.py").write_text("print('99% accuracy')", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "修改"):
            exp.search(True)

    def test_tampered_artifact_is_rejected(self):
        exp = self.start()
        result = exp.evaluate({"degree": 1}, "dev", True)
        write_json(exp.home / "runs" / result["run_id"] / "predictions-11.json", [])
        with self.assertRaisesRegex(ProtocolError, "修改"):
            exp.replay()

    def test_tampered_events_are_rejected(self):
        exp = self.start()
        with exp.db:
            exp.db.execute("UPDATE events SET payload='{}' WHERE seq=1")
        with self.assertRaisesRegex(ProtocolError, "事件链"):
            exp.replay()

    def test_duplicate_split_samples_are_rejected(self):
        train = read_json(self.root / "train.json")
        dev = read_json(self.root / "dev.json")
        dev[0] = {**train[0], "id": "different-id"}
        write_json(self.root / "dev.json", dev)
        with self.assertRaisesRegex(ProtocolError, "相同"):
            initialize(self.root)

    def test_split_id_overlap_is_rejected(self):
        dev = read_json(self.root / "dev.json")
        dev[0]["id"] = "train-0"
        write_json(self.root / "dev.json", dev)
        with self.assertRaisesRegex(ProtocolError, "交集"):
            initialize(self.root)

    def test_no_unregistered_or_self_reported_metric(self):
        spec = read_json(self.root / "experiment.json")
        spec["metric"] = {"name": "f1", "direction": "max"}
        write_json(self.root / "experiment.json", spec)
        with self.assertRaises(ProtocolError):
            initialize(self.root)
        with self.assertRaises(ProtocolError):
            score([{"id": "a", "x": 1, "y": 2}], [{"id": "a", "prediction": 2, "metric": 99}],
                  "mse-v1")

    def test_predictions_are_matched_by_id(self):
        rows = [{"id": "a", "x": 1, "y": 2}, {"id": "b", "x": 2, "y": 4}]
        self.assertEqual(0, score(rows, [{"id": "b", "prediction": 4}, {"id": "a", "prediction": 2}],
                                  "mse-v1"))
        for predictions in [[{"id": "a", "prediction": float("nan")}],
                            [{"id": "a", "prediction": 2}, {"id": "a", "prediction": 4}],
                            [{"id": "a", "prediction": True}, {"id": "b", "prediction": 4}]]:
            with self.assertRaises(ProtocolError):
                score(rows, predictions, "mse-v1")

    def test_binary_accuracy_is_controller_scored(self):
        rows = [{"id": "a", "features": [1.0, 2.0], "label": 1},
                {"id": "b", "features": [3.0, 4.0], "label": 0}]
        value = score(rows, [{"id": "b", "prediction": 0}, {"id": "a", "prediction": 1}],
                      "binary-accuracy-v1")
        self.assertEqual(1.0, value)
        with self.assertRaises(ProtocolError):
            score(rows, [{"id": "a", "prediction": 0.9}, {"id": "b", "prediction": 0}],
                  "binary-accuracy-v1")

    def test_budget_and_resume_dont_repeat_completed_runs(self):
        spec = read_json(self.root / "experiment.json")
        spec["budget"] = 1
        write_json(self.root / "experiment.json", spec)
        exp = self.start()
        exp.search(True)
        exp.search(True)
        self.assertEqual(2, len(exp.results()))
        with self.assertRaisesRegex(ProtocolError, "预算"):
            exp.evaluate({"degree": 2}, "dev", True)

    def test_failed_candidate_spends_budget(self):
        spec = read_json(self.root / "experiment.json")
        spec["budget"] = 1
        write_json(self.root / "experiment.json", spec)
        exp = self.start()
        exp.evaluate(spec["baseline"], "dev", True)
        with patch("popper.sandbox.launch", side_effect=OSError("failure")):
            with self.assertRaises(ProtocolError):
                exp.evaluate(spec["candidates"][0], "dev", True)
        with self.assertRaisesRegex(ProtocolError, "预算"):
            exp.evaluate(spec["candidates"][1], "dev", True)

    def test_untrusted_local_requires_explicit_flag(self):
        exp = self.start()
        with self.assertRaises(ProtocolError):
            exp.search()
        self.assertEqual([], exp.results())

    def test_proposer_only_sees_dev_results(self):
        exp = self.start()
        calls = []
        def proposer(remaining, feedback, objective):
            calls.append(feedback)
            self.assertTrue(all(r["split"] == "dev" for r in feedback))
            return {"index": len(remaining) - 1, "hypothesis": "更高阶特征可能降低开发集误差"}
        exp.search(True, proposer)
        self.assertEqual(3, len(calls))
        self.assertEqual({"degree": 3}, exp.results()[1]["config"])

    def test_invalid_proposal_does_not_execute_candidate(self):
        exp = self.start()
        with self.assertRaises(ProtocolError):
            exp.search(True, lambda *_: {"index": -1, "hypothesis": "bad"})
        self.assertEqual(1, len(exp.results()))

    def test_path_traversal_is_rejected(self):
        spec = read_json(self.root / "experiment.json")
        spec["train"] = "../secrets.json"
        write_json(self.root / "experiment.json", spec)
        with self.assertRaises(ProtocolError):
            initialize(self.root)

    def test_recovery_consumes_interrupted_test(self):
        exp = self.start()
        state = exp.state()
        state["phase"] = "confirming"
        with exp.db:
            exp._save(state, "test_consumed", {})
        exp.recover()
        self.assertEqual("confirmation_failed", exp.state()["phase"])
        with self.assertRaises(ProtocolError):
            exp.confirm(True)

    def test_provider_rejects_insecure_remote_transport(self):
        with self.assertRaises(ProtocolError):
            make_proposer("http://example.org/v1", "model")

    def test_sandboxed_requires_available_backend(self):
        exp = self.start()
        with patch("popper.core.sandbox.available", return_value=False):
            with self.assertRaises(ProtocolError):
                exp.evaluate({"degree": 1}, "dev", sandboxed=True)
        self.assertEqual([], exp.results("dev"))

    def test_sandboxed_run_records_os_sandbox_trust(self):
        from popper import sandbox as sandbox_module
        real_launch = sandbox_module.launch

        def delegate(command, cwd, env, stdout, stderr, timeout_seconds,
                     mem_limit_mb=None, cpu_time_seconds=None, max_processes=None,
                     sandboxed=False, on_spawn=None):
            # 沙箱分支被替换为 trusted-local 的真实执行：本测试只验证 trust 标记。
            real_launch(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                        timeout_seconds=timeout_seconds, mem_limit_mb=mem_limit_mb,
                        cpu_time_seconds=cpu_time_seconds, sandboxed=False)

        exp = self.start()
        with patch("popper.core.sandbox.available", return_value=True), \
                patch("popper.core.sandbox.label_write_scope", return_value=None), \
                patch("popper.core.sandbox.unlabel_write_scope", return_value=None), \
                patch("popper.sandbox.launch", side_effect=delegate):
            result = exp.evaluate({"degree": 1}, "dev", sandboxed=True)
        self.assertEqual("controller_scored_os_sandbox", result["trust"])

    def test_test_split_seals_registered_holdout_labels(self):
        """--sandbox 消费测试集时，已注册的测试数据集在候选执行窗口内被封读。

        封读的内核行为由 tests/test_sandbox.py 实测；这里断言编排确实把它接在了
        测试消费窗口上，且开发集搜索阶段不封读（否则会连自己在读的划分一起封掉）。
        """
        from popper import sandbox as sandbox_module
        real_launch, real_sealed_reads = sandbox_module.launch, sandbox_module.sealed_reads
        sealed = []

        def spy(paths):
            recorded = [Path(p) for p in paths]
            if recorded:
                sealed.append(recorded)
            return real_sealed_reads(paths)

        def delegate(command, cwd, env, stdout, stderr, timeout_seconds,
                     mem_limit_mb=None, cpu_time_seconds=None, max_processes=None,
                     sandboxed=False, on_spawn=None):
            real_launch(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                        timeout_seconds=timeout_seconds, mem_limit_mb=mem_limit_mb,
                        cpu_time_seconds=cpu_time_seconds, sandboxed=False)

        exp = self.start()
        with patch("popper.core.sandbox.available", return_value=True), \
                patch("popper.core.sandbox.seal_read_available", return_value=True), \
                patch("popper.core.sandbox.label_write_scope", return_value=None), \
                patch("popper.core.sandbox.unlabel_write_scope", return_value=None), \
                patch("popper.core.sandbox.sealed_reads", side_effect=spy), \
                patch("popper.sandbox.launch", side_effect=delegate):
            exp.search(True)
            exp.freeze()
            claim = exp.confirm(sandboxed=True)
        self.assertEqual("supports_threshold", claim["status"])
        holdout = (self.root / "test.json").resolve()
        # 基线与冻结候选各一次测试运行；dev 搜索的 4 次运行不封读。
        self.assertEqual([[holdout], [holdout]], sealed)

    def test_unsealable_holdout_fails_before_consuming_the_test_access(self):
        """封读不可用时 --sandbox 直接失败，且不消耗只有一次的测试访问。"""
        exp = self.start()
        exp.search(True)
        exp.freeze()
        with patch("popper.core.sandbox.available", return_value=True), \
                patch("popper.core.sandbox.seal_read_available", return_value=False):
            with self.assertRaisesRegex(ProtocolError, "禁读"):
                exp.confirm(sandboxed=True)
            with self.assertRaisesRegex(ProtocolError, "禁读"):
                exp.evaluate({"degree": 1}, "test", sandboxed=True)
        self.assertEqual("frozen", exp.state()["phase"])
        self.assertEqual([], exp.results("test"))
        # 人工路径不受影响：封读只约束 --sandbox 的隔离主张。
        self.assertEqual("supports_threshold", exp.confirm(True)["status"])

    def test_cli_rejects_dual_exec_mode(self):
        exp = self.start()
        with patch("popper.core.sandbox.available", return_value=True):
            code = main(["experiment", "search", str(self.root),
                         "--trusted-local", "--sandbox"])
        self.assertEqual(2, code)
        self.assertEqual([], exp.results("dev"))

    def test_workstation_requires_host_and_session_token(self):
        self.start()
        server = Workstation(("127.0.0.1", 0), self.root, False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            # 前端未构建时 `/` 必须显式 503（不回退任何内嵌 HTML）。已构建才断言 HTML：
            # 分支不是放宽守卫，而是把两种状态都钉住——干净克隆里没有 dist，
            # 只断言 HTML 的写法会把一个正确的 fail-closed 行为误判成测试失败。
            dist_built = (Path(popper_server.__file__).resolve().parents[1]
                          / "frontend" / "dist" / "index.html").is_file()
            if dist_built:
                with urllib.request.urlopen(base + "/", timeout=2) as response:
                    page = response.read().decode()
                self.assertIn("<html", page.lower())
            else:
                with self.assertRaises(urllib.error.HTTPError) as unbuilt:
                    urllib.request.urlopen(base + "/", timeout=2)
                self.assertEqual(503, unbuilt.exception.code)
                self.assertIn("前端未构建",
                              unbuilt.exception.read().decode("utf-8", errors="replace"))
            # token 通过 /api/session 引导端点返回，不再注入 HTML（React 前端）
            with urllib.request.urlopen(base + "/api/session", timeout=2) as response:
                session = json.loads(response.read())
            self.assertEqual(server.token, session["token"])
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base + "/api/state", timeout=2)
            self.assertEqual(403, denied.exception.code)
            request = urllib.request.Request(base + "/api/state", headers={"X-Popper-Token": server.token})
            with urllib.request.urlopen(request, timeout=2) as response:
                state = json.loads(response.read())
            self.assertEqual("searching", state["state"]["phase"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
