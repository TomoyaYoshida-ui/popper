import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.observability import Tracer
from popper.web_glue import render_shell


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "run"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tracer = Tracer(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_trace_writes_jsonl_and_spans_read_back(self):
        self.tracer.trace({"type": "llm", "model": "x", "token_in": 10,
                           "token_out": 5, "latency_ms": 120, "event": "propose"})
        self.tracer.trace({"type": "agent", "model": "y", "token_in": 3,
                           "token_out": 1, "latency_ms": 40, "event": "grade"})
        spans = self.tracer.spans()
        self.assertEqual(2, len(spans))
        self.assertIn("token_in", spans[0])
        self.assertEqual("propose", spans[0]["event"])
        self.assertTrue((self.dir / ".popper-obs" / "events.jsonl").is_file())

    def test_privacy_scan_rejects_key_secret_field(self):
        with self.assertRaisesRegex(ProtocolError, "凭据"):
            self.tracer.trace({"type": "llm", "model": "x", "api_key": "s3cret",
                               "token_in": 1, "token_out": 1, "event": "noop"})
        clean = self.tracer.trace({"type": "llm", "model": "x", "token_in": 1,
                                   "token_out": 1, "latency_ms": 1, "event": "ok"})
        self.assertIn("token_in", clean)
        self.assertTrue(self.tracer.privacy_scan())

    def test_privacy_scan_sees_secret_in_existing_span(self):
        self.tracer.trace({"type": "llm", "model": "x", "token_in": 1, "event": "ok"})
        (self.dir / ".popper-obs" / "events.jsonl").write_text(
            '{"type":"llm","Authorization":"Bearer abc","event":"leaked"}\n',
            encoding="utf-8")
        self.assertFalse(self.tracer.privacy_scan())


class WebGlueTests(unittest.TestCase):
    def test_render_shell_contains_shell_div_and_items(self):
        html = render_shell("Demo", [{"title": "alpha"}, {"title": "beta"}])
        self.assertIn('<div id="shell">', html)
        self.assertIn("alpha", html)
        self.assertIn("beta", html)

    def test_render_shell_escapes_title(self):
        html = render_shell("A <script>", [])
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn(">A <script></", html)


if __name__ == "__main__":
    unittest.main()