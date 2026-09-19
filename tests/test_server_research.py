"""工作台假设环 HTTP 测试：/api/research 只读投影真实研究 run。

验证三件事：
- 端点遵循工作台鉴权（无 token 403）；
- 未登记 / 未初始化目录的诚实呈现（空列表与 ok:false，不造假数据）；
- 投影内容来自 research.sqlite 的真实快照与事件链，且 HTTP 读取全程
  对研究库零写入（只读打开，事件链与库文件字节不变化）。
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
from popper.research import ResearchController
from popper.server import Workstation

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


def _make_project(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(EXAMPLE / name, root / name)
    runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](root)
    initialize(root)
    return root


class ResearchLoopHttpTests(unittest.TestCase):
    def _start(self, research_names=()):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self.tmp.name) / "project")
        self.research_dirs = [Path(self.tmp.name) / name for name in research_names]
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  research_runs=self.research_dirs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as r:
            self.token = json.loads(r.read())["token"]

    def tearDown(self):
        if hasattr(self, "server"):
            # 监控线程不登记、等不到，只等真实后台作业；其余同工作台测试约定。
            leftover = self.server.wait_for_jobs(timeout=60)
            self.assertEqual(0, leftover, "后台作业线程没收尾，临时目录会被继续写入")
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def _get(self, path, token=True):
        headers = {"X-Popper-Token": self.token} if token else {}
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _db_fingerprint(self, run_dir):
        """库文件字节级指纹 + 事件数：HTTP 读取前后必须一致（零写入）。"""
        db = Path(run_dir) / "research.sqlite"
        with open(db, "rb") as f:
            return db.stat().st_size, hash(f.read()), db.stat().st_mtime_ns

    def test_research_endpoint_requires_session_token(self):
        self._start()
        code, body = self._get("/api/research", token=False)
        self.assertEqual(403, code)
        self.assertIn("error", body)

    def test_no_registered_runs_is_empty_and_labeled(self):
        self._start()
        code, body = self._get("/api/research")
        self.assertEqual(200, code)
        self.assertEqual([], body["runs"])
        labels = body["status_labels"]
        self.assertTrue(labels)
        self.assertEqual("未实验", labels["hypothesis"]["untested"])
        self.assertEqual("预注册", labels["origin"]["created"])
        self.assertEqual("安排对照", labels["action"]["add_control"])

    def test_registered_run_projects_real_state_without_writing(self):
        self._start(research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        before = self._db_fingerprint(run_dir)

        code, body = self._get("/api/research")
        self.assertEqual(200, code)
        self.assertEqual(1, len(body["runs"]))
        run = body["runs"][0]
        self.assertEqual(str(run_dir.resolve()), run["run_dir"])
        self.assertTrue(run["ok"])
        # 二次回归示例：1 条对照假设 + 3 条预注册候选
        self.assertEqual(4, len(run["hypotheses"]))
        statuses = {h["status"] for h in run["hypotheses"]}
        self.assertEqual({"untested"}, statuses)
        # 每条假设都有 created 版本事件；尚无实验证据
        self.assertEqual(4, len(run["revisions"]))
        self.assertTrue(all(r["origin"] == "created" for r in run["revisions"]))
        self.assertEqual([], run["observations"])
        self.assertEqual([], run["decisions"])
        self.assertEqual([], run["reflections"])
        self.assertIsNotNone(run["study"])
        self.assertEqual("active", run["study"]["status"])
        self.assertGreaterEqual(run["state_version"], 1)
        budget = run["budget"]
        self.assertIsNotNone(budget)
        self.assertGreater(budget["cap"], 0)
        self.assertEqual(budget["cap"], budget["available"])
        self.assertEqual("registered_hypothesis_baseline",
                         run["manifest"]["capability_mode"])
        # 只读保证：HTTP 投影全程没有改变研究库一个字节
        self.assertEqual(before, self._db_fingerprint(run_dir))

    def test_real_run_surfaces_observations_and_decisions(self):
        self._start(research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        controller = ResearchController(run_dir)
        try:
            status = controller.run(trusted_local=True, auto_confirm=True)
        finally:
            controller.close()
        before = self._db_fingerprint(run_dir)

        code, body = self._get("/api/research")
        self.assertEqual(200, code)
        run = body["runs"][0]
        self.assertTrue(run["ok"])
        self.assertTrue(status["observations"])
        self.assertTrue(run["observations"], "开发实验已真实执行，投影必须有观察")
        for observation in run["observations"]:
            self.assertTrue(observation["hypothesis_id"])
            self.assertTrue(observation["scope"].startswith(("dev", "confirmation")))
            self.assertIsNotNone(observation["value"])
        self.assertTrue(run["decisions"], "真实运行必须产生决策记录")
        allowed = set(body["status_labels"]["action"])
        self.assertTrue(all(d["action"] in allowed for d in run["decisions"]))
        self.assertEqual(before, self._db_fingerprint(run_dir))


if __name__ == "__main__":
    unittest.main()
