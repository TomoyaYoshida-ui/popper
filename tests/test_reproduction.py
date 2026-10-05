import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from popper.core import ProtocolError
from popper.reproduction import (ReproductionTask, initialize_reproduction,
                                 inspect_reproduction, reproduction_research_projection)
from popper.server import Workstation


class ReproductionTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        metric = {"name": "accuracy", "direction": "max", "unit": "fraction"}
        paper = {
            "schema_version": "1.0", "title": "Fixture paper", "authors": ["A. Author"],
            "year": 2020, "venue": "Fixture venue", "doi": "10.0000/fixture",
            "landing_page": "https://example.org/paper",
            "claim": {"id": "PAPER-C001", "text": "Accuracy was 75%.",
                      "metric": metric, "reported_value": 0.75,
                      "evidence": {"location": "Abstract", "source_url": "https://example.org/paper",
                                   "support": "direct"}},
        }
        protocol = {
            "schema_version": "1.0", "frozen_before_execution": True,
            "dataset": {"name": "Fixture data", "official_url": "https://example.org/data",
                        "expected_sha256": "0" * 64},
            "target_claim": {"id": "PAPER-C001", "metric": metric,
                             "reported_value": 0.75, "absolute_tolerance": 0.05},
        }
        (self.root / "paper.json").write_text(json.dumps(paper), encoding="utf-8")
        (self.root / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
        (self.root / "runner.py").write_text(
            "import json\nfrom pathlib import Path\n"
            "Path('predictions.json').write_text(json.dumps([1, 1, 0, 1]))\n"
            "Path('results.json').write_text(json.dumps({'accuracy': 0.75}))\n",
            encoding="utf-8")
        (self.root / "verifier.py").write_text(
            "import json\nfrom pathlib import Path\n"
            "predictions=json.loads(Path('predictions.json').read_text())\n"
            "result=json.loads(Path('results.json').read_text())\n"
            "assert sum(predictions)/len(predictions) == result['accuracy']\n"
            "print(json.dumps({'status': 'verified', 'accuracy': result['accuracy']}))\n",
            encoding="utf-8")
        manifest = {
            "schema_version": "1.0", "id": "fixture-c001", "title": "Fixture claim",
            "paper_record": "paper.json", "protocol": "protocol.json",
            "runner": "runner.py", "verifier": "verifier.py",
            "required_outputs": ["predictions.json", "results.json"],
            "timeout_seconds": 30,
        }
        (self.root / "reproduction.json").write_text(
            json.dumps(manifest), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_generic_reproduction_runs_and_verifies(self):
        readiness = inspect_reproduction(self.root)
        self.assertEqual("ready", readiness["status"])
        self.assertEqual("PAPER-C001", readiness["claim_id"])
        initialize_reproduction(self.root)
        task = ReproductionTask(self.root)
        result = task.run(trusted_local=True)
        self.assertEqual("verified", result["phase"])
        self.assertEqual(0.75, result["verification"]["accuracy"])
        self.assertEqual("supports_reproduction", result["assessment"]["outcome"])
        self.assertEqual(0.0, result["assessment"]["absolute_difference"])
        self.assertNotIn("result_field", result["assessment"])
        self.assertEqual("verified", task.audit()["phase"])
        projection = reproduction_research_projection(self.root)
        self.assertTrue(projection["experiment_graph"]["integrity"]["ok"])
        self.assertEqual("supports_reproduction", projection["decisions"][0]["action"])
        self.assertEqual(0.75, projection["observations"][0]["observed_value"])
        self.assertTrue({"study", "external_source", "reproduction_protocol",
                         "code_revision", "run", "artifact", "observation", "decision"}
                        <= {node["kind"] for node in
                            projection["experiment_graph"]["nodes"]})
        with self.assertRaisesRegex(ProtocolError, "只能执行一次"):
            task.run(trusted_local=True)

    def test_frozen_primary_metric_alias_is_explicit_in_assessment(self):
        verifier = self.root / "verifier.py"
        verifier.write_text(
            "import json\nprint(json.dumps({'status': 'verified', "
            "'primary_accuracy': 0.75}))\n", encoding="utf-8")
        initialize_reproduction(self.root)
        task = ReproductionTask(self.root)
        result = task.run(trusted_local=True)
        self.assertEqual("supports_reproduction", result["assessment"]["outcome"])
        self.assertEqual("primary_accuracy", result["assessment"]["result_field"])
        self.assertEqual("verified", task.audit()["phase"])

    def test_tampered_output_and_frozen_input_are_rejected(self):
        initialize_reproduction(self.root)
        task = ReproductionTask(self.root)
        task.run(trusted_local=True)
        (self.root / "results.json").write_text('{"accuracy": 1}\n', encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "产物"):
            task.audit()
        (self.root / "results.json").write_text('{"accuracy": 0.75}', encoding="utf-8")
        (self.root / "protocol.json").write_text('{"threshold": 0.9}\n', encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "输入"):
            task.audit()

    def test_code_execution_requires_explicit_trust(self):
        initialize_reproduction(self.root)
        with self.assertRaisesRegex(ProtocolError, "trusted-local"):
            ReproductionTask(self.root).run()

    def test_protocol_must_target_the_recorded_paper_claim(self):
        protocol = json.loads((self.root / "protocol.json").read_text())
        protocol["target_claim"]["reported_value"] = 0.9
        (self.root / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "主张不一致"):
            inspect_reproduction(self.root)

    def test_verified_output_can_honestly_fail_to_reproduce_claim(self):
        (self.root / "runner.py").write_text(
            "import json\nfrom pathlib import Path\n"
            "Path('predictions.json').write_text(json.dumps([0, 0, 0, 1]))\n"
            "Path('results.json').write_text(json.dumps({'accuracy': 0.25}))\n",
            encoding="utf-8")
        initialize_reproduction(self.root)
        result = ReproductionTask(self.root).run(trusted_local=True)
        self.assertEqual("verified", result["phase"])
        self.assertEqual("does_not_reproduce", result["assessment"]["outcome"])
        self.assertEqual(0.5, result["assessment"]["absolute_difference"])

    def test_verifier_cannot_omit_registered_metric(self):
        (self.root / "verifier.py").write_text(
            "import json\nprint(json.dumps({'status': 'verified', 'other': 0.75}))\n",
            encoding="utf-8")
        initialize_reproduction(self.root)
        task = ReproductionTask(self.root)
        with self.assertRaisesRegex(ProtocolError, "必须包含有限数值指标"):
            task.run(trusted_local=True)
        self.assertEqual("verification_failed", task.state()["phase"])

    def test_json_projection_is_not_authoritative(self):
        initialize_reproduction(self.root)
        task = ReproductionTask(self.root)
        task.run(trusted_local=True)
        state_path = self.root / ".popper-reproduction/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["verification"]["assessment"]["outcome"] = "does_not_reproduce"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        audited = task.audit()
        self.assertEqual("supports_reproduction",
                         audited["verification"]["assessment"]["outcome"])
        state_path.unlink()
        self.assertEqual("verified", ReproductionTask(self.root).state()["phase"])

    def test_lifecycle_is_an_audited_event_chain(self):
        initialize_reproduction(self.root)
        task = ReproductionTask(self.root)
        task.run(trusted_local=True)
        history = task.event_history()
        self.assertTrue(history["integrity"]["ok"])
        self.assertEqual(
            ["registered", "execution_started", "execution_completed",
             "verification_started", "verification_completed"],
            [event["event_type"] for event in history["events"]])

    def test_event_tampering_is_rejected(self):
        initialize_reproduction(self.root)
        task = ReproductionTask(self.root)
        task.run(trusted_local=True)
        database = self.root / ".popper-reproduction/reproduction.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.execute("UPDATE reproduction_events SET payload='{}' WHERE seq=2")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(ProtocolError, "完整性失败"):
            task.audit()

    def test_concurrent_runs_have_one_transactional_winner(self):
        runner = self.root / "runner.py"
        runner.write_text("import json, time\nfrom pathlib import Path\n"
                          "time.sleep(0.2)\n"
                          "Path('predictions.json').write_text(json.dumps([1, 1, 0, 1]))\n"
                          "Path('results.json').write_text(json.dumps({'accuracy': 0.75}))\n",
                          encoding="utf-8")
        initialize_reproduction(self.root)

        def invoke():
            try:
                return ReproductionTask(self.root).run(trusted_local=True)["phase"]
            except ProtocolError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: invoke(), range(2)))
        self.assertEqual(["rejected", "verified"], sorted(outcomes))
        events = ReproductionTask(self.root).event_history()["events"]
        self.assertEqual(1, sum(event["event_type"] == "execution_started"
                                for event in events))

    def test_legacy_json_is_imported_once(self):
        expected = initialize_reproduction(self.root)
        database = self.root / ".popper-reproduction/reproduction.sqlite"
        database.unlink()
        task = ReproductionTask(self.root)
        self.assertEqual(expected, task.state())
        history = task.event_history()
        self.assertEqual(["legacy_imported"],
                         [event["event_type"] for event in history["events"]])
        self.assertTrue(history["integrity"]["ok"])

    def test_restart_records_interrupted_execution(self):
        initialize_reproduction(self.root)
        task = ReproductionTask(self.root)
        state = task.state()
        state["phase"] = "running"
        task.events_store.transition({"registered"}, state, "execution_started")
        server = Workstation(("127.0.0.1", 0), self.root, False)
        try:
            recovered = ReproductionTask(self.root)
            self.assertEqual("execution_interrupted", recovered.state()["phase"])
            self.assertEqual("interrupted",
                             recovered.event_history()["events"][-1]["event_type"])
        finally:
            server.server_close()

    def test_second_workstation_cannot_interrupt_live_reproduction(self):
        initialize_reproduction(self.root)
        server = Workstation(("127.0.0.1", 0), self.root, False)
        try:
            task = ReproductionTask(self.root)
            state = task.state()
            state["phase"] = "running"
            task.events_store.transition({"registered"}, state, "execution_started")
            with self.assertRaisesRegex(ProtocolError, "已有运行中"):
                Workstation(("127.0.0.1", 0), self.root, False)
            self.assertEqual("running", task.state()["phase"])
            self.assertEqual("execution_started",
                             task.event_history()["events"][-1]["event_type"])
        finally:
            server.server_close()

    def test_closed_listener_keeps_lease_while_background_job_runs(self):
        initialize_reproduction(self.root)
        server = Workstation(("127.0.0.1", 0), self.root, False)
        release = threading.Event()
        server._start_job(lambda: release.wait(timeout=5))
        try:
            server.server_close()
            with self.assertRaisesRegex(ProtocolError, "已有运行中"):
                Workstation(("127.0.0.1", 0), self.root, False)
        finally:
            release.set()
            server._lease_reaper.join(timeout=3)
        self.assertFalse(server._lease_reaper.is_alive())
        reopened = Workstation(("127.0.0.1", 0), self.root, False)
        reopened.server_close()

    def test_failed_workstation_start_releases_lease(self):
        initialize_reproduction(self.root)
        with self.assertRaisesRegex(ProtocolError, "必须同时提供"):
            Workstation(("127.0.0.1", 0), self.root, chat_base_url="http://127.0.0.1/v1")
        reopened = Workstation(("127.0.0.1", 0), self.root, False)
        reopened.server_close()

    def test_workstation_exposes_verified_reproduction(self):
        initialize_reproduction(self.root)
        ReproductionTask(self.root).run(trusted_local=True)
        server = Workstation(("127.0.0.1", 0), self.root, False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/state",
                headers={"X-Popper-Token": server.token})
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read())
            self.assertEqual("reproduction", payload["mode"])
            self.assertEqual("verified", payload["state"]["phase"])
            self.assertEqual(0.75, payload["result"]["accuracy"])
            self.assertEqual(2, len(payload["artifacts"]))
            self.assertTrue(payload["reproduction_integrity"]["ok"])
            self.assertEqual("verification_completed", payload["events"][0]["kind"])
            projection = payload["research_projection"]
            self.assertTrue(projection["experiment_graph"]["integrity"]["ok"])
            self.assertEqual("supports_reproduction", projection["decisions"][0]["action"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
