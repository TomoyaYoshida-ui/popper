import json
import runpy
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from popper.core import initialize
from popper.server import Workstation


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class ProjectFilesHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.root / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.root)
        initialize(self.root)
        (self.root / "notes.md").write_text("first", encoding="utf-8")

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
        self.temporary.cleanup()

    def start(self, trusted):
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  trusted_local=trusted)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as response:
            self.token = json.load(response)["token"]

    def request(self, path, payload=None, *, token=True):
        headers = {"X-Popper-Token": self.token} if token else {}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_trusted_code_editor_lists_reads_and_conflict_checks(self):
        self.start(True)
        code, listed = self.request("/api/v1/project-files")
        self.assertEqual(200, code)
        self.assertEqual("primary", listed["data"]["workspace_id"])
        entries = {item["path"]: item for item in listed["data"]["files"]}
        self.assertFalse(entries["model.py"]["editable"])
        self.assertTrue(entries["notes.md"]["editable"])
        read_path = "/api/v1/project-files?path=notes.md&workspace_id=primary"
        code, original = self.request(read_path)
        self.assertEqual(200, code)
        self.assertEqual("first", original["data"]["content"])
        payload = {"schema_version": "1.0", "workspace_id": "primary",
                   "path": "notes.md", "content": "second",
                   "expected_sha256": original["data"]["sha256"]}
        code, preview = self.request("/api/v1/project-files/preview", payload)
        self.assertEqual(200, code)
        self.assertTrue(preview["data"]["changed"])
        self.assertEqual("first", (self.root / "notes.md").read_text(encoding="utf-8"))
        code, saved = self.request("/api/v1/project-files", payload)
        self.assertEqual(200, code)
        self.assertEqual(preview["data"]["after_sha256"], saved["data"]["sha256"])
        self.assertEqual("second", (self.root / "notes.md").read_text(encoding="utf-8"))
        self.assertNotEqual(original["data"]["sha256"], saved["data"]["sha256"])
        code, stale = self.request("/api/v1/project-files", payload)
        self.assertEqual(409, code)
        code, stale_preview = self.request("/api/v1/project-files/preview", payload)
        self.assertEqual(409, code)
        self.assertIn("别处修改", stale["error"]["message"])
        self.assertEqual("second", (self.root / "notes.md").read_text(encoding="utf-8"))
        code, switched = self.request("/api/v1/project-files",
                                      {**payload, "workspace_id": "wrong"})
        self.assertEqual(409, code)
        self.assertIn("工作区已切换", switched["error"]["message"])
        code, model = self.request(
            "/api/v1/project-files?path=model.py&workspace_id=primary")
        self.assertFalse(model["data"]["editable"])
        code, frozen = self.request("/api/v1/project-files", {
            **payload, "path": "model.py", "expected_sha256": model["data"]["sha256"]})
        self.assertEqual(400, code)
        self.assertIn("冻结", frozen["error"]["message"])
        code, traversal = self.request(
            "/api/v1/project-files?path=" + urllib.parse.quote("../secret.py"))
        self.assertEqual(400, code)
        self.assertIn("路径", traversal["error"]["message"])

    def test_untrusted_and_unauthenticated_file_api_is_denied(self):
        self.start(False)
        code, _ = self.request("/api/v1/project-files")
        self.assertEqual(403, code)
        code, _ = self.request("/api/v1/project-files", {
            "schema_version": "1.0", "workspace_id": "primary", "path": "notes.md",
            "content": "forged", "expected_sha256": "0" * 64})
        self.assertEqual(403, code)
        code, _ = self.request("/api/v1/project-files/preview", {
            "schema_version": "1.0", "workspace_id": "primary", "path": "notes.md",
            "content": "forged", "expected_sha256": "0" * 64})
        self.assertEqual(403, code)
        self.assertEqual("first", (self.root / "notes.md").read_text(encoding="utf-8"))
