"""工作台交互审批 HTTP 测试：挂起区块展示 + 批准/驳回 + 后台恢复执行调用链。"""
import json
import runpy
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from popper.capabilities import extra_available
from popper.core import initialize
from popper.server import Workstation

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"

# 小步骤集：dev → variant(审批点) → freeze → confirm → materialize；
# approve 后回放全部节点 → completed（materialize 可在审批时透传 config 真实物化）
CAMPAIGN_STEPS = [
    {"key": "dev", "needs": []},
    {"key": "variant", "needs": [], "approval": "proposal_approved"},
    {"key": "freeze", "needs": ["dev"]},
    {"key": "confirm", "needs": ["freeze"]},
    {"key": "materialize", "needs": ["confirm"]},
]


def _mk_campaign(run_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0", "objective": "工作台审批测试",
        "status": "waiting_approval", "steps": CAMPAIGN_STEPS,
        "current": "variant",
        "history": [{"step": "variant", "outcome": "pending_approval",
                     "result": {"required": "proposal_approved"}}],
        "waiting_approval": {"step": "variant", "required": "proposal_approved",
                             "reason": "需要在工作台人工审阅 proposal 后批准恢复"},
    }
    (run_dir / "campaign.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                          encoding="utf-8")
    prop = run_dir / "proposal"
    prop.mkdir(parents=True, exist_ok=True)
    (prop / "proposal.json").write_text(
        json.dumps({"edits": [{"file": "model.py", "note": "+1 惰性正则"}]}),
        encoding="utf-8")
    (prop / "proposal.diff").write_text(
        "--- a/model.py\n+++ b/model.py\n@@ -1,1 +1,2 @@\n-  old\n+  new\n", encoding="utf-8")


def _make_project(temp_root):
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(EXAMPLE / name, temp_root / name)
    runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](temp_root)
    initialize(temp_root)
    return temp_root


class FakeNodes:
    """离线 fake 节点：全部 success，验证后台恢复的确被调用回放。"""

    def __init__(self):
        self.calls = []

    def node(self, key):
        def fn(run_dir, state):
            self.calls.append(key)
            return {"outcome": "success", "key": key}
        return fn


@unittest.skipUnless(extra_available("orchestration"),
                     "orchestration extra 未安装（langgraph）：审批后的 resume 跑真实编译图")
class CampaignApprovalHttpTests(unittest.TestCase):
    trusted_local = False

    def _start(self, trusted=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self.tmp.name))
        self.camp = Path(self.tmp.name) / "campaign"
        _mk_campaign(self.camp)
        self.server = Workstation(("127.0.0.1", 0), self.root, trusted,
                                  campaign_dir=str(self.camp))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as r:
            self.token = json.loads(r.read())["token"]

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
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

    def test_get_campaign_exposes_pending_and_evidence(self):
        self._start()
        code, body = self._get("/api/campaign")
        self.assertEqual(200, code)
        self.assertIn("phase", body)
        ap = body["campaign"]
        self.assertIsNotNone(ap)
        self.assertEqual("variant", ap["step"])
        self.assertEqual("proposal_approved", ap["required"])
        self.assertEqual("waiting_approval", ap["status"])
        self.assertIn("diff", ap["evidence"])
        self.assertIn("proposal", ap["evidence"])
        # 不配置 --campaign 无挂起区块
        self.tmp2 = tempfile.TemporaryDirectory()
        r2 = _make_project(Path(self.tmp2.name))
        s2 = Workstation(("127.0.0.1", 0), r2, False, campaign_dir=None)
        t2 = threading.Thread(target=s2.serve_forever, daemon=True)
        t2.start()
        base2 = f"http://127.0.0.1:{s2.server_address[1]}"
        with urllib.request.urlopen(base2 + "/api/session", timeout=3) as rr:
            tok2 = json.loads(rr.read())["token"]
        req = urllib.request.Request(base2 + "/api/campaign",
                                     headers={"X-Popper-Token": tok2})
        with urllib.request.urlopen(req, timeout=5) as r:
            body2 = json.loads(r.read())
        self.assertIsNone(body2["campaign"])
        s2.shutdown(); s2.server_close(); t2.join(timeout=2)
        self.tmp2.cleanup()

    def test_approve_requires_trusted_local(self):
        self._start(trusted=False)
        code, body = self._post("/api/campaign", {"action": "approve"},
                                expect_error=True)
        self.assertEqual(409, code)
        self.assertIn("--trusted-local", body["error"])

    def test_reject_writes_approval_without_execute(self):
        self._start(trusted=True)
        code, body = self._post("/api/campaign", {"action": "reject", "reason": "方向不符"})
        self.assertEqual(200, code)
        self.assertEqual("rejected", body["decision"])
        approvals = json.loads((self.camp / "approvals.json").read_text(encoding="utf-8"))
        record = approvals["records"][0]
        self.assertEqual("rejected", record["decision"])
        self.assertEqual("方向不符", record["reason"])
        # 未执行任何节点
        self.assertEqual("idle", self.server.campaign_job["status"])

    def test_approve_resumes_in_background(self):
        self._start(trusted=True)
        nodes = FakeNodes()
        self.server._campaign_nodes = {k: nodes.node(k)
                                       for k in ("dev", "variant", "freeze", "confirm", "materialize")}
        code, body = self._post("/api/campaign", {"action": "approve", "reason": "ok"})
        self.assertEqual(200, code)
        self.assertEqual("approved", body["decision"])
        # 后台恢复完成：轮询 campaign 状态
        deadline = time.time() + 8
        while time.time() < deadline:
            state = json.loads((self.camp / "campaign.json").read_text(encoding="utf-8"))
            if state.get("status") in {"completed", "failed"}:
                break
            time.sleep(0.1)
        state = json.loads((self.camp / "campaign.json").read_text(encoding="utf-8"))
        self.assertEqual("completed", state["status"])
        # 审批记录落盘
        approvals = json.loads((self.camp / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual("approved", approvals["records"][0]["decision"])
        # campaign_job 完成且记录了结果
        self.assertEqual("completed", self.server.campaign_job["status"])
        self.assertEqual("approved", self.server.campaign_job["decision"])
        # 三个节点都被重新回放
        self.assertEqual(["dev", "variant", "freeze", "confirm", "materialize"],
                         nodes.calls)

    def test_approve_rejects_bad_materialize(self):
        self._start(trusted=True)
        # manuscript 非 dict → 拒绝
        code, body = self._post("/api/campaign", {"action": "approve",
                                                  "materialize": {"manuscript": "x"}},
                                expect_error=True)
        self.assertEqual(409, code)
        self.assertIn("manuscript", body["error"])
        # 未知模板 → 拒绝
        code2, body2 = self._post("/api/campaign", {
            "action": "approve",
            "materialize": {"manuscript": {}, "template": "odt"},
        }, expect_error=True)
        self.assertEqual(409, code2)
        self.assertIn("模板", body2["error"])

    def test_approve_with_materialize_realizes_manuscript(self):
        """批准时透传 materialize → 恢复周期内真实物化稿件到 deliverables。"""
        self._start(trusted=True)  # 默认 BUILTIN_NODES，真实 dev/freeze/confirm/materialize
        # 移除 proposal，让 variant（code-materialize）如实跳过，专注验证 materialize 透传
        shutil.rmtree(self.camp / "proposal")
        materialize = {
            "manuscript": {"title": "审批物化冒烟", "abstract": "摘要",
                           "sections": [{"heading": "方法", "body": "正文"}]},
            "template": "md", "disclosure": "nature",
        }
        code, body = self._post("/api/campaign", {"action": "approve", "reason": "ok",
                                                  "materialize": materialize})
        self.assertEqual(200, code)
        deadline = time.time() + 60
        while time.time() < deadline:
            st = json.loads((self.camp / "campaign.json").read_text(encoding="utf-8"))
            if st.get("status") in {"completed", "failed"}:
                break
            time.sleep(0.2)
        st = json.loads((self.camp / "campaign.json").read_text(encoding="utf-8"))
        self.assertEqual("completed", st["status"])
        # 稿件真正物化并登记到 workspace
        md = self.root / "deliverables" / "manuscript.md"
        self.assertTrue(md.is_file())
        self.assertIn("AI 使用披露", md.read_text(encoding="utf-8"))
        with (self.root / ".popper" / "workspace.json").open(encoding="utf-8") as f:
            ws = json.load(f)
        self.assertEqual(1, len(ws["deliverables"]))

    # ---- 声称点边界：输入契约、状态、并发、失败、持久化 ----

    def test_post_campaign_input_validation(self):
        self._start(trusted=True)
        cases = [
            ({"action": "nope"}, "approve 或 reject"),
            ({"action": "approve", "reason": 5}, "reason 必须为字符串"),
            ({"action": "approve", "materialize": []}, "materialize 必须为对象"),
            ({"action": "approve", "materialize": {"manuscript": "x"}}, "manuscript 必须是对象"),
            ({"action": "approve", "materialize": {"manuscript": {}, "disclosure": "self"}},
             "披露声明口径"),
        ]
        for payload, needle in cases:
            code, body = self._post("/api/campaign", payload, expect_error=True)
            self.assertEqual(409, code, payload)
            self.assertIn(needle, body["error"], payload)
        # payload 本身非对象（list）→ 拒绝，不 500
        code, body = self._post("/api/campaign", ["x"], expect_error=True)
        self.assertEqual(409, code)
        self.assertIn("action", body["error"])

    def test_post_campaign_requires_configured_run_dir(self):
        """未配置 --campaign 时 approve/reject 都被拒绝。"""
        self._start(trusted=True)
        self.server.campaign_dir = None
        for action in ("approve", "reject"):
            code, body = self._post("/api/campaign", {"action": action}, expect_error=True)
            self.assertEqual(409, code, action)
            self.assertIn("未配置", body["error"])

    def test_approve_rejects_when_not_waiting(self):
        self._start(trusted=True)
        # 非挂起态（completed，无 waiting_approval）→ 拒绝
        finished = {"schema_version": "1.0", "status": "completed", "steps": CAMPAIGN_STEPS,
                    "history": []}
        (self.camp / "campaign.json").write_text(json.dumps(finished, ensure_ascii=False),
                                                 encoding="utf-8")
        code, body = self._post("/api/campaign", {"action": "approve"}, expect_error=True)
        self.assertEqual(409, code)
        self.assertIn("不在挂起审批状态", body["error"])
        # manifest 缺失 → 拒绝（不崩成 500）
        (self.camp / "campaign.json").replace(self.camp / "campaign.json.bak")
        code, body = self._post("/api/campaign", {"action": "approve"}, expect_error=True)
        self.assertEqual(409, code)
        self.assertIn("缺少 manifest", body["error"])
        (self.camp / "campaign.json.bak").replace(self.camp / "campaign.json")

    def test_reject_allowed_without_trusted_local(self):
        """驳回无副作用，不需要 --trusted-local。"""
        self._start(trusted=False)
        code, body = self._post("/api/campaign", {"action": "reject", "reason": "no"})
        self.assertEqual(200, code)
        self.assertEqual("rejected", body["decision"])
        approvals = json.loads((self.camp / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual("rejected", approvals["records"][0]["decision"])

    def test_approve_background_failure_marks_job_failed(self):
        """后台恢复节点抛异常 → campaign_job=failed，campaign 不虚报完成。"""
        self._start(trusted=True)

        def boom(run_dir, state):
            raise RuntimeError("boom")

        self.server._campaign_nodes = {k: boom for k in
                                       ("dev", "variant", "freeze", "confirm", "materialize")}
        code, body = self._post("/api/campaign", {"action": "approve"})
        self.assertEqual(200, code)
        deadline = time.time() + 8
        while time.time() < deadline:
            job = self.server.campaign_job
            if job.get("status") == "failed":
                break
            time.sleep(0.1)
        self.assertEqual("failed", self.server.campaign_job["status"])
        self.assertEqual("approved", self.server.campaign_job["decision"])
        self.assertIn("boom", self.server.campaign_job["error"])
        # campaign.json 未被错误地推进到 completed
        st = json.loads((self.camp / "campaign.json").read_text(encoding="utf-8"))
        self.assertNotEqual("completed", st["status"])
        # 已写审批记录（决定本身保留）
        approvals = json.loads((self.camp / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual("approved", approvals["records"][0]["decision"])

    def test_campaign_lock_blocks_concurrent_approve(self):
        self._start(trusted=True)
        gate = threading.Event()

        def block(run_dir, state):
            gate.wait(timeout=5)
            return {"outcome": "success"}

        self.server._campaign_nodes = {k: block for k in
                                       ("dev", "variant", "freeze", "confirm", "materialize")}
        # 首次 approve 后台持锁（阻塞在节点里）
        code, body = self._post("/api/campaign", {"action": "approve"})
        self.assertEqual(200, code)
        deadline = time.time() + 8
        while time.time() < deadline:
            if not self.server.campaign_lock.acquire(blocking=False):
                break  # 锁已被占用
            self.server.campaign_lock.release()
            time.sleep(0.05)
        # 锁被占用期间再次审批 → 拒绝
        code2, body2 = self._post("/api/campaign", {"action": "approve"}, expect_error=True)
        self.assertEqual(409, code2)
        self.assertIn("正在执行", body2["error"])
        gate.set()

    def test_approval_records_accumulate_and_corrupt_reset(self):
        self._start(trusted=True)
        # 多次审批记录累积
        self._post("/api/campaign", {"action": "reject", "reason": "r1"})
        self._post("/api/campaign", {"action": "reject", "reason": "r2"})
        approvals = json.loads((self.camp / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(2, len(approvals["records"]))
        # 损坏的 approvals.json（非 dict）→ 复位为新的 records 容器而非崩
        (self.camp / "approvals.json").write_text("[1, 2]", encoding="utf-8")
        self._post("/api/campaign", {"action": "reject", "reason": "r3"})
        approvals_after = json.loads((self.camp / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(approvals_after["records"]))
        self.assertEqual("r3", approvals_after["records"][0]["reason"])

    def test_get_campaign_edges(self):
        """GET：manifest 缺失 / 非法 JSON / 非挂起态 → campaign 为 null（不报错）。"""
        self._start()
        # 非挂起态
        finished = {"schema_version": "1.0", "status": "completed", "steps": CAMPAIGN_STEPS,
                    "history": []}
        (self.camp / "campaign.json").write_text(json.dumps(finished, ensure_ascii=False),
                                                 encoding="utf-8")
        _, body = self._get("/api/campaign")
        self.assertIsNone(body["campaign"])
        # 非法 JSON
        (self.camp / "campaign.json").write_text("not json {{", encoding="utf-8")
        _, body2 = self._get("/api/campaign")
        self.assertIsNone(body2["campaign"])
        # manifest 缺失
        (self.camp / "campaign.json").rename(self.camp / "campaign.json.bak")
        _, body3 = self._get("/api/campaign")
        self.assertIsNone(body3["campaign"])
        (self.camp / "campaign.json.bak").rename(self.camp / "campaign.json")


if __name__ == "__main__":
    unittest.main()