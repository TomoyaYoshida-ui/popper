import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from popper.core import initialize
from popper.server import Workstation


class ProviderConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1] / "examples/quadratic"
        shutil.copytree(source, root / "project", ignore=shutil.ignore_patterns(".popper"))
        initialize(root / "project")
        self.project = root / "project"
        self.server = Workstation(("127.0.0.1", 0), self.project)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method="GET", body=None, *, token=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Popper-Token"] = self.server.token
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(self.base + "/api/v1/provider-config", method=method,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)["data"]

    def test_authentication_and_ephemeral_credentials(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request(token=False)
        self.assertEqual(403, denied.exception.code)
        secret = "example-process-only-key"
        value = {"schema_version": "1.0", "base_url": "http://127.0.0.1:1234/v1",
                 "model": "local-test", "api_key": secret, "token_budget": 50000,
                 "cost_budget_micros": 10500000}
        saved = self.request("PUT", value)
        self.assertTrue(saved["configured"])
        self.assertTrue(saved["credential_available"])
        self.assertNotIn(secret, json.dumps(saved))
        self.assertNotIn(secret, (self.project / ".popper/provider-config.json").read_text(encoding="utf-8"))
        self.assertEqual("workspace", self.request()["source"])
        with self.assertRaises(urllib.error.HTTPError) as crossed:
            self.request("POST", value, origin="https://unrelated.example")
        self.assertEqual(403, crossed.exception.code)
        session = self.server.create_agent_session("configured")
        from popper.sessions import AgentSessionStore
        store = AgentSessionStore(self.project)
        try:
            self.assertEqual(50000, store.usage_summary(session["session_id"])["token_budget"])
        finally:
            store.close()

    def test_running_turn_keeps_its_original_adapter(self):
        service = self.server._service("providers")
        def factory(base, model, **kwargs):
            return lambda system, messages: model
        with patch("popper.services.providers.make_chat_client", side_effect=factory):
            for model in ("first", "second"):
                service.dispatch("save", {"schema_version": "1.0", "base_url": "http://localhost:1234/v1",
                    "model": model, "api_key": "example-only"})
                if model == "first":
                    self.server._turn_harnesses["old-turn"] = self.server.chat_harness
            reply = self.server._invoke_budgeted_harness(None, "old-turn", "system", [], None, lambda value: None)
            self.assertEqual("first", reply)
            self.assertEqual("second", self.server.chat_model)

    def test_cli_configuration_wins_over_saved_defaults(self):
        self.server._provider_cli_base = "http://localhost:1234/v1"
        self.server._provider_cli_model = "cli-model"
        value = {"schema_version": "1.0", "base_url": "http://localhost:1235/v1",
                 "model": "workspace-model", "api_key": "ephemeral", "token_budget": None}
        result = self.request("POST", value)
        self.assertEqual("cli", result["source"])
        self.assertEqual("cli-model", result["model"])
        self.assertEqual("workspace-model", result["saved_defaults"]["model"])


class MonitorRecoveryTests(unittest.TestCase):
    def test_slow_subscriber_keeps_updates_from_other_domains(self):
        import queue
        server = object.__new__(Workstation)
        subscriber = queue.Queue(maxsize=1)
        server._subscribers = {subscriber}
        context = {"workspace_id": "one", "navigation_version": 1}
        server._broadcast({"context": context, "state": {"value": 1}})
        server._broadcast({"context": context, "workspace": {"value": 2}})
        patch = json.loads(subscriber.get_nowait().decode()[6:])
        self.assertEqual({"value": 1}, patch["state"])
        self.assertEqual({"value": 2}, patch["workspace"])

    def test_transient_payload_error_retries_without_another_file_change(self):
        from unittest.mock import Mock
        server = object.__new__(Workstation)
        server.workspaces = Mock()
        server.workspaces.current_wid.return_value = "one"
        server.navigation_version = 1
        server.chat_lock = threading.Lock()
        server._monitor_stop = Mock()
        server._monitor_stop.wait.side_effect = [False, False, True]
        baseline = {"workspace": "old"}
        server._fingerprints = Mock(side_effect=[baseline, {"workspace": "new"}, {"workspace": "new"}])
        server.workspace_payload = Mock(side_effect=[OSError("temporary"), {"ok": True}])
        server.state_payload = server.campaign_payload = server.research_payload = server.chat_payload = Mock()
        server._broadcast = Mock()
        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                self.target()
        with patch("popper.server.threading.Thread", InlineThread):
            server._start_monitor()
        self.assertEqual(2, server.workspace_payload.call_count)
        self.assertEqual({"ok": True}, server._broadcast.call_args.args[0]["workspace"])
        self.assertEqual({}, server.monitor_errors)
