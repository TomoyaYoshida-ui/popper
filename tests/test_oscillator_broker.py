import os
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.research.oscillator_broker import RHSQuerySession, WindowsRHSBroker
from popper.research.oscillator_client import RHSClient


def row(identity="case"):
    return {"id": identity, "system": "harmonic", "omega": 2.0,
            "q0": 0.3, "v0": 0.0, "t_end": 1.0,
            "budget": 256, "times": [0, 0.5, 1.0]}


class RHSProtocolTests(unittest.TestCase):
    def request(self, broker, kind, **fields):
        return broker.request({"capability": broker.capability, "job_id": broker.job_id,
                               "seq": broker.sequence, "kind": kind, "case_id": "case", **fields})

    def test_host_counts_queries_and_accepts_only_tracks(self):
        broker = RHSQuerySession("job", [row()])
        self.request(broker, "begin")
        self.assertEqual([0.0, -1.2], self.request(broker, "rhs", t=0, state=[0.3, 0.0])["value"])
        self.request(broker, "finish", states=[[0.3, 0.0]] * 3)
        receipt = broker.receipt()
        self.assertEqual(1, receipt["cases"][0]["query_count"])
        self.assertEqual("succeeded", receipt["cases"][0]["status"])
        self.assertNotEqual("0" * 64, receipt["query_chain_sha256"])

    def test_forged_count_fails_not_used_as_receipt(self):
        broker = RHSQuerySession("job", [row()])
        self.request(broker, "begin")
        with self.assertRaises(ProtocolError):
            self.request(broker, "finish", states=[[0.3, 0.0]] * 3, query_count=0)
        self.assertIsNone(broker.receipt()["payload"]["trajectories"][0]["states"])

    def test_scope_sequence_capability_and_unknown_case(self):
        for change in ({"job_id": "other"}, {"seq": 1}, {"capability": "wrong"},
                       {"case_id": "other"}, {"seq": True}):
            broker = RHSQuerySession("job", [row()])
            message = {"capability": broker.capability, "job_id": "job", "seq": 0,
                       "kind": "begin", "case_id": "case", **change}
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                broker.request(message)
            self.assertEqual(0, broker.receipt()["cases"][0]["query_count"])

    def test_exact_budget_last_query_succeeds_next_fails(self):
        broker = RHSQuerySession("job", [row()])
        self.request(broker, "begin")
        for _ in range(256):
            self.request(broker, "rhs", t=0, state=[0.3, 0])
        with self.assertRaises(ProtocolError):
            self.request(broker, "rhs", t=0, state=[0.3, 0])
        receipt = broker.receipt()
        self.assertEqual(256, receipt["cases"][0]["query_count"])
        self.assertEqual("failed", receipt["cases"][0]["status"])

    def test_disconnect_does_not_invent_completed_remaining_case(self):
        broker = RHSQuerySession("job", [row(), row("second")])
        self.request(broker, "begin")
        self.request(broker, "finish", states=[[0.3, 0.0]] * 3)
        broker.fail("disconnected")
        receipt = broker.receipt()
        self.assertIsNotNone(receipt["payload"]["trajectories"][0]["states"])
        self.assertIsNone(receipt["payload"]["trajectories"][1]["states"])

    def test_invalid_rhs_cannot_access_arbitrary_attributes(self):
        for fields in ({"t": 0, "state": [0.3, 0], "path": "reference.json"},
                       {"t": -1, "state": [0, 0]}, {"t": 0, "state": [float("nan"), 0]}):
            broker = RHSQuerySession("job", [row()])
            self.request(broker, "begin")
            with self.subTest(fields=fields), self.assertRaises(ProtocolError):
                self.request(broker, "rhs", **fields)


@unittest.skipUnless(os.name == "nt", "Windows named-pipe transport")
class WindowsTransportTests(unittest.TestCase):
    def test_real_named_pipe_roundtrip_and_disconnect(self):
        broker = WindowsRHSBroker("job", [row()], timeout=5)
        client = RHSClient(broker.address, broker.session.capability, "job", timeout=2)
        try:
            client.begin("case")
            self.assertEqual([0.0, -1.2], client.rhs(0, [0.3, 0.0]))
            client.finish([[0.3, 0.0]] * 3)
            self.assertTrue(broker.done.wait(2))
            self.assertIsNone(broker.session.error)
        finally:
            client.close()
            broker.close()

    def test_unconnected_broker_cancels_pending_accept(self):
        broker = WindowsRHSBroker("job", [row()], timeout=5)
        broker.close()
        self.assertTrue(broker.done.is_set())

    def test_shutdown_before_accept_registers_io(self):
        import threading
        from unittest.mock import patch
        entered, release = threading.Event(), threading.Event()
        original = WindowsRHSBroker._accept

        def delayed(instance):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test gate was not released")
            return original(instance)

        with patch.object(WindowsRHSBroker, "_accept", delayed):
            broker = WindowsRHSBroker("job", [row()], timeout=5)
            self.assertTrue(entered.wait(1))
            closer = threading.Thread(target=broker.close)
            closer.start()
            self.assertTrue(broker.stopping.wait(1))
            release.set()
            closer.join(2)
            self.assertFalse(closer.is_alive())
            self.assertTrue(broker.done.is_set())
            self.assertFalse(broker.thread.is_alive())
            self.assertIn("cancelled before connection", broker.session.error)

    def test_unconnected_accept_observes_job_deadline(self):
        broker = WindowsRHSBroker("job", [row()], timeout=0.1)
        self.assertTrue(broker.done.wait(2))
        broker.close()
        self.assertIn("deadline exhausted", broker.session.error)

    def test_repeated_immediate_shutdown(self):
        for index in range(30):
            with self.subTest(index=index):
                broker = WindowsRHSBroker("job", [row()], timeout=5)
                broker.close()
                broker.close()
                self.assertTrue(broker.done.is_set())
                self.assertFalse(broker.thread.is_alive())

    def test_candidate_low_integrity_connects_without_reading_host(self):
        from popper import sandbox
        if not sandbox.available():
            self.skipTest("OS sandbox unavailable")
        import json
        import sys
        broker = WindowsRHSBroker("job", [row()], timeout=15)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "client.py"
            client_source = Path(__file__).resolve().parents[1] / "popper" / "research" / "oscillator_client.py"
            source.write_text(client_source.read_text(encoding="utf-8") + "\n" +
                              "import sys\nc=RHSClient(sys.argv[1],sys.argv[2],'job')\n"
                              "c.begin('case')\nassert c.rhs(0,[0.3,0.0]) == [0.0,-1.2]\n"
                              "c.finish([[0.3,0.0]]*3)\nc.close()\n", encoding="utf-8")
            output, errors = root / "stdout.log", root / "stderr.log"
            sandbox.label_write_scope(root)
            try:
                environment = {k: v for k, v in os.environ.items()
                               if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
                with output.open("wb") as stdout, errors.open("wb") as stderr:
                    sandbox.launch([sys.executable, str(source), broker.address, broker.session.capability],
                                   cwd=str(root), env=environment, stdout=stdout, stderr=stderr,
                                   timeout_seconds=10, sandboxed=True)
                self.assertTrue(broker.done.wait(2))
                self.assertIsNone(broker.session.error)
                self.assertEqual(1, broker.session.receipt()["cases"][0]["query_count"])
            finally:
                broker.close()
                sandbox.unlabel_write_scope(root)


if __name__ == "__main__":
    unittest.main()
