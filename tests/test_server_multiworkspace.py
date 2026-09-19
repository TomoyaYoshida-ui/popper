"""工作台多工作区（多个 workspace 根）HTTP 测试。

验证五件事：
- /api/workspace 下发真实根注册表：主根 = serve 的项目目录，永在且当前；
- 额外根只能显式登记（Workstation workspace_roots），登记持久化、重启不丢、重复幂等；
- workspace_switch / workspace_remove 的语义与防护（未知 wid、主根不可移除、
  摘除当前根回退主根）；
- 根间数据隔离：每个根的 Quest 只存在于该根自己的 workspace.json；
- 切换会改变 SSE workspace 指纹（注册表 + 当前根 workspace.json），前端由此收到补丁。
"""
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


def _make_project(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(EXAMPLE / name, root / name)
    runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](root)
    initialize(root)
    return root


class MultiWorkspaceHttpTests(unittest.TestCase):
    def _start(self, workspace_roots=()):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self.tmp.name) / "project")
        self.extra_dirs = [Path(self.tmp.name) / name
                           for name in ("ws-a", "ws-b")]
        for d in self.extra_dirs:
            d.mkdir(parents=True, exist_ok=True)
        roots = tuple(self.extra_dirs[i] for i in workspace_roots)
        self.server = Workstation(("127.0.0.1", 0), self.root, workspace_roots=roots)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as r:
            self.token = json.loads(r.read())["token"]

    def _restart(self):
        """模拟重启：同一项目目录再起一个 Workstation（不重复登记 CLI 根）。"""
        self._stop()
        self.server = Workstation(("127.0.0.1", 0), self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as r:
            self.token = json.loads(r.read())["token"]

    def _stop(self):
        leftover = self.server.wait_for_jobs(timeout=60)
        self.assertEqual(0, leftover, "后台作业线程没收尾，临时目录会被继续写入")
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def tearDown(self):
        if hasattr(self, "server"):
            self._stop()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def _get(self, path):
        req = urllib.request.Request(self.base + path,
                                     headers={"X-Popper-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"X-Popper-Token": self.token,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # -- 根注册表与载荷 ---------------------------------------------------------

    def test_payload_lists_primary_root_with_real_name(self):
        self._start()
        code, body = self._get("/api/workspace")
        self.assertEqual(200, code)
        roots = body["roots"]
        self.assertEqual(1, len(roots))
        primary = roots[0]
        self.assertTrue(primary["primary"])
        self.assertTrue(primary["current"])
        self.assertEqual("primary", primary["wid"])
        self.assertEqual(str(self.root.resolve()), primary["root"])
        # 显示名来自真实 experiment.json 的 name（后端派生，不造占位名）
        spec = json.loads((self.root / "experiment.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["name"], primary["name"])
        self.assertEqual("primary", body["current_root"])

    def test_register_cli_roots_persist_and_dedupe(self):
        self._start(workspace_roots=(0, 0))
        code, body = self._get("/api/workspace")
        self.assertEqual(200, code)
        roots = body["roots"]
        self.assertEqual(2, len(roots), "重复登记必须幂等去重")
        extra = roots[1]
        self.assertFalse(extra["primary"])
        self.assertEqual("ws-a", extra["name"], "非项目根显示名 = 目录名")
        self.assertEqual(str(self.extra_dirs[0].resolve()), extra["root"])
        # 切到额外根后重启：登记与当前根指针都持久化，重启不丢
        self._post("/api/workspace", {"op": "workspace_switch", "wid": extra["wid"]})
        self._restart()
        code, body = self._get("/api/workspace")
        self.assertEqual(200, code)
        self.assertEqual(2, len(body["roots"]))
        self.assertEqual(extra["wid"], body["current_root"])

    def test_register_missing_root_is_rejected(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self.tmp.name) / "project")
        missing = Path(self.tmp.name) / "nope"
        with self.assertRaisesRegex(Exception, "工作区根不存在或不是目录"):
            Workstation(("127.0.0.1", 0), self.root, workspace_roots=(missing,))
        afile = self.root / "experiment.json"
        with self.assertRaisesRegex(Exception, "工作区根不存在或不是目录"):
            Workstation(("127.0.0.1", 0), self.root, workspace_roots=(afile,))

    # -- 切换 / 移除 ------------------------------------------------------------

    def test_switch_isolates_quest_data_per_root(self):
        self._start(workspace_roots=(0,))
        code, body = self._get("/api/workspace")
        roots = body["roots"]
        primary_wid, extra_wid = roots[0]["wid"], roots[1]["wid"]
        fid = body["folders"][0]["fid"]
        code, body = self._post("/api/workspace", {"op": "task_create", "fid": fid,
                                                   "title": "主根任务"})
        self.assertEqual(200, code)

        # 切到额外根：全新种子（单文件夹单任务），主根任务不可见
        code, body = self._post("/api/workspace", {"op": "workspace_switch",
                                                   "wid": extra_wid})
        self.assertEqual(200, code)
        self.assertEqual(extra_wid, body["current_root"])
        self.assertEqual("ws-a", body["roots"][1]["name"])
        self.assertTrue(all("主根任务" != t["title"]
                            for f in body["folders"] for t in f["tasks"]))
        fid2 = body["folders"][0]["fid"]
        self._post("/api/workspace", {"op": "task_create", "fid": fid2,
                                      "title": "副根任务"})

        # 切回主根：主根任务仍在（各根自己的 workspace.json 是唯一状态源）
        code, body = self._post("/api/workspace", {"op": "workspace_switch",
                                                   "wid": primary_wid})
        self.assertEqual(200, code)
        self.assertEqual(primary_wid, body["current_root"])
        titles = [t["title"] for f in body["folders"] for t in f["tasks"]]
        self.assertIn("主根任务", titles)
        self.assertNotIn("副根任务", titles)
        # 副根数据落盘在副根目录
        ws_a = json.loads((self.extra_dirs[0] / ".popper" / "workspace.json")
                          .read_text(encoding="utf-8"))
        self.assertIn("副根任务",
                      [t["title"] for f in ws_a["folders"] for t in f["tasks"]])

    def test_switch_unknown_wid_rejected(self):
        self._start()
        code, body = self._post("/api/workspace", {"op": "workspace_switch",
                                                   "wid": "w_nope"})
        self.assertEqual(409, code)
        self.assertIn("工作区不存在", body["error"])

    def test_remove_guards_and_current_fallback(self):
        self._start(workspace_roots=(0, 1))
        code, body = self._get("/api/workspace")
        roots = body["roots"]
        primary_wid = roots[0]["wid"]
        extra_wids = [r["wid"] for r in roots[1:]]

        # 主根不可移除
        code, body = self._post("/api/workspace", {"op": "workspace_remove",
                                                   "wid": primary_wid})
        self.assertEqual(409, code)
        self.assertIn("主工作区不可移除", body["error"])
        # 未知 wid 拒绝
        code, body = self._post("/api/workspace", {"op": "workspace_remove",
                                                   "wid": "w_nope"})
        self.assertEqual(409, code)
        # 移除当前根（切到 extra[0] 再摘除）→ 回退主根；磁盘数据保留
        self._post("/api/workspace", {"op": "workspace_switch", "wid": extra_wids[0]})
        code, body = self._post("/api/workspace", {"op": "workspace_remove",
                                                   "wid": extra_wids[0]})
        self.assertEqual(200, code)
        self.assertEqual(primary_wid, body["current_root"])
        self.assertEqual(2, len(body["roots"]))
        self.assertTrue((self.extra_dirs[0] / ".popper" / "workspace.json").is_file(),
                        "移除只是从侧边栏摘除，磁盘数据必须保留")
        # 未摘除的根仍在列
        self.assertEqual({extra_wids[1]}, {r["wid"] for r in body["roots"]
                                           if not r["primary"]})

    # -- SSE 联动 ---------------------------------------------------------------

    def test_switch_changes_workspace_fingerprint(self):
        self._start(workspace_roots=(0,))
        before = self.server._fingerprints()["workspace"]
        extra_wid = self.server.workspaces.roots_payload()[1]["wid"]
        self.server.workspaces.switch(extra_wid)
        after = self.server._fingerprints()["workspace"]
        self.assertNotEqual(before, after,
                            "切换必须改变 SSE workspace 指纹，前端才能收到补丁")
        # 内容变化同样触发：往当前根 workspace.json 写入
        before = after
        with self.server.ws_lock:
            data = self.server.workspace.read()
            self.server.workspace.save(data)
        self.assertNotEqual(before, self.server._fingerprints()["workspace"])


if __name__ == "__main__":
    unittest.main()
