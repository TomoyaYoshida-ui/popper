import json
import runpy
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from popper.core import initialize
from popper.server import Workstation

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class MaterializeHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.root / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.root)
        initialize(self.root)
        self.server = Workstation(("127.0.0.1", 0), self.root, False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as r:
            self.token = json.loads(r.read())["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _get(self, path):
        req = urllib.request.Request(self.base + path, headers={"X-Popper-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get_bytes(self, path):
        req = urllib.request.Request(self.base + path, headers={"X-Popper-Token": self.token})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()

    def _post(self, path, payload, expect_error=False):
        req = urllib.request.Request(
            self.base + path, method="POST",
            headers={"X-Popper-Token": self.token, "Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            if expect_error:
                return e.code, json.loads(e.read())
            raise

    def test_workspace_seed_and_ops_over_http(self):
        status, ws = self._get("/api/workspace")
        self.assertEqual(200, status)
        self.assertEqual(1, len(ws["folders"]))
        fid = ws["folders"][0]["fid"]
        # 新建文件夹 + 新 Quest（全局计数）
        self._post("/api/workspace", {"op": "folder_create", "name": "分队"})
        _, ws2 = self._get("/api/workspace")
        target_fid = ws2["folders"][-1]["fid"]
        self._post("/api/workspace", {"op": "task_create", "fid": target_fid})
        _, ws3 = self._get("/api/workspace")
        tasks = ws3["folders"][-1]["tasks"]
        self.assertEqual(1, len(tasks))
        self.assertEqual("新 Quest 1", tasks[0]["title"])
        # 未知 op → 409
        code, body = self._post("/api/workspace", {"op": "nope", "name": "x"}, expect_error=True)
        self.assertEqual(409, code)
        self.assertIn("未知工作区操作", body["error"])

    def test_materialize_md_and_download(self):
        manuscript = {"title": "HTTP 物化测试", "abstract": "摘要",
                      "sections": [{"heading": "方法", "body": "正文"}]}
        status, res = self._post("/api/materialize", {
            "manuscript": manuscript, "template": "md", "disclosure": "nature"})
        self.assertEqual(200, status)
        item = res["item"]
        self.assertEqual("md", item["template"])
        self.assertGreater(item["bytes"], 0)
        self.assertTrue((self.root / "deliverables" / item["filename"]).is_file())
        # 下载
        status2, headers, body_bytes = self._get_bytes(f"/api/file?item={item['id']}")
        self.assertEqual(200, status2)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("AI 使用披露", body_bytes.decode("utf-8"))
        # 产物出现在 GET /api/workspace 的 deliverables 列表
        _, ws = self._get("/api/workspace")
        self.assertEqual(1, len(ws["deliverables"]))

    def test_file_writeback_and_traversal_guard(self):
        """POST /api/file 仅覆写 deliverables 内已存在 md/tex/json，防穿越/防新建。"""
        manuscript = {"title": "t", "abstract": "a",
                      "sections": [{"heading": "方法", "body": "b"}]}
        _, res = self._post("/api/materialize", {
            "manuscript": manuscript, "template": "md", "disclosure": "nature"})
        item_id = res["item"]["id"]
        # 合法写回已存在的 md → 200
        code, body = self._post("/api/file", {"item": item_id, "content": "# 修改后"})
        self.assertEqual(200, code)
        self.assertGreater(body["bytes"], 0)
        ws_path = self.root / ".popper" / "workspace.json"
        data = json.loads(ws_path.read_text(encoding="utf-8"))
        entry = next(it for it in data["deliverables"] if it["id"] == item_id)
        # 内容真实落盘
        fp = self.root / "deliverables" / entry["filename"]
        self.assertEqual("# 修改后", fp.read_text(encoding="utf-8"))
        # 篡改登记文件名越界（../）→ 409 拒绝
        entry["filename"] = "../secret.md"
        ws_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code2, body2 = self._post("/api/file", {"item": item_id, "content": "x"},
                                  expect_error=True)
        self.assertEqual(409, code2)
        self.assertIn("产物路径越界", body2["error"])
        # 未登记 item → 拒绝
        code3, body3 = self._post("/api/file", {"item": "d_forged", "content": "x"},
                                  expect_error=True)
        self.assertEqual(409, code3)
        self.assertIn("产物不存在", body3["error"])

    def test_materialize_validation_409(self):
        base = {"template": "md", "disclosure": "nature"}
        bad = [
            {"op": "materialize", **base, "manuscript": "not-dict"},
            {"op": "materialize", "manuscript": {}, "template": "odt", "disclosure": "nature"},
            {"op": "materialize", "manuscript": {}, "template": "md", "disclosure": "self"},
        ]
        for payload in bad:
            code, body = self._post("/api/materialize", payload, expect_error=True)
            self.assertEqual(409, code, payload)
            self.assertIn("error", body)

    def test_file_download_rejects_unknown_item(self):
        code, body = self._get("/api/file?item=d_forged")
        self.assertEqual(400, code)
        self.assertIn("产物不存在", body["error"])


if __name__ == "__main__":
    unittest.main()