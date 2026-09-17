import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from popper.core import ProtocolError
from popper.reproduction import ReproductionTask, initialize_reproduction, inspect_reproduction
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
        self.assertEqual("verified", task.audit()["phase"])
        with self.assertRaisesRegex(ProtocolError, "只能执行一次"):
            task.run(trusted_local=True)

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
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
