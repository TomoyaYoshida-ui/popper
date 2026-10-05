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
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from popper.core import ProtocolError, digest, file_hash, initialize, read_json, write_json
from popper.research import ResearchController
from popper.research.contracts import ExperimentDesign, Hypothesis
from popper.research.design_jobs import DesignExecutionJobStore
from popper.research.revisions import CodeEdit, RevisionStore
from popper.research.store import ResearchStore
from popper.server import Workstation

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


def _make_project(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(EXAMPLE / name, root / name)
    runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](root)
    initialize(root)
    return root


class StudyOnboardingNonMlTests(unittest.TestCase):
    def test_ode_study_uses_same_workbench_initialization_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "ode"
            source = EXAMPLE.parent / "ode-convergence"
            spec = read_json(source / "experiment.json")
            project.mkdir()
            names = {"experiment.json", spec["train"], spec["dev"], spec["test"],
                     *spec["code_files"], *spec.get("provenance_files", [])}
            for name in names:
                destination = project / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, destination)
            initialize(project)
            server = Workstation(("127.0.0.1", 0), project, trusted_local=True)
            try:
                preview = server.preview_study_initialization("ode-study", 5)
                created = server.initialize_study_from_workbench(
                    "ode-study", 5, preview["proposal_sha256"])
                state = read_json(Path(created["run_dir"]) / "research.json")
                self.assertEqual("independent_run", state["repeat_unit"])
                self.assertEqual("ode-study", Path(created["run_dir"]).name)
                self.assertTrue(server.research_payload()["runs"][0]["ok"])
            finally:
                server.server_close()
            reopened = Workstation(("127.0.0.1", 0), project, trusted_local=False)
            try:
                self.assertEqual(1, len(reopened.research_payload()["runs"]))
                self.assertEqual(created["study_id"], reopened.research_payload()[
                    "runs"][0]["manifest"]["study_id"])
            finally:
                reopened.server_close()


class ResearchLoopHttpTests(unittest.TestCase):
    def test_study_initialization_preview_confirm_and_durable_registration(self):
        self._start(trusted_local=True)
        envelope = {"schema_version": "1.0", "run_name": "first-study",
                    "budget_cap": 8}
        code, denied = self._post(
            "/api/v1/research/studies/preview", envelope, token=False)
        self.assertEqual(403, code)
        code, preview = self._post("/api/v1/research/studies/preview", envelope)
        self.assertEqual(200, code)
        self.assertFalse(preview["data"]["already_initialized"])
        run_dir = Path(preview["data"]["run_dir"])
        self.assertFalse(run_dir.exists())
        code, stale = self._post("/api/v1/research/studies", {
            **envelope, "proposal_sha256": "0" * 64})
        self.assertEqual(400, code)
        self.assertFalse(run_dir.exists())
        model_path = self.root / "model.py"
        original_model = model_path.read_bytes()
        try:
            model_path.write_bytes(original_model + b"\n# concurrent edit\n")
            code, changed = self._post("/api/v1/research/studies", {
                **envelope, "proposal_sha256": preview["data"]["proposal_sha256"]})
            self.assertEqual(400, code)
            self.assertFalse(changed["ok"])
            self.assertFalse(run_dir.exists())
        finally:
            model_path.write_bytes(original_model)
        code, created = self._post("/api/v1/research/studies", {
            **envelope, "proposal_sha256": preview["data"]["proposal_sha256"]})
        self.assertEqual(201, code)
        self.assertTrue((run_dir / "research.sqlite").is_file())
        self.assertEqual(run_dir, Path(created["data"]["run_dir"]))
        code, state = self._get("/api/v1/research")
        self.assertEqual(200, code)
        self.assertEqual(1, len(state["data"]["runs"]))
        self.assertTrue(state["data"]["runs"][0]["experiment_graph"]["integrity"]["ok"])
        self.assertEqual(str(run_dir), read_json(
            self.root / ".popper" / "research-runs.json")["runs"][0])
        code, repeated = self._post("/api/v1/research/studies", {
            **envelope, "proposal_sha256": preview["data"]["proposal_sha256"]})
        self.assertEqual(201, code)
        self.assertTrue(repeated["data"]["already_initialized"])
        self.assertEqual(created["data"]["study_id"], repeated["data"]["study_id"])

    def test_initial_revision_job_recovery_does_not_need_worktree_registry(self):
        from popper.research.execution_jobs import RevisionExecutionJobStore

        self._start(research_names=("research",), trusted_local=True)
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        revision_id = "REV-" + "a" * 24
        jobs = RevisionExecutionJobStore(run_dir / "revision-execution-jobs.sqlite")
        try:
            job = jobs.enqueue(f"initial:{revision_id}", revision_id)
            jobs.claim(job["job_id"])
        finally:
            jobs.close()
        self.server._recover_revision_execution_jobs()
        jobs = RevisionExecutionJobStore(run_dir / "revision-execution-jobs.sqlite")
        try:
            self.assertEqual("interrupted", jobs.get(job["job_id"])["status"])
            self.assertTrue(jobs.verify()["ok"])
        finally:
            jobs.close()
        self.assertFalse((run_dir / "revision-worktrees.sqlite").exists())
        jobs = RevisionExecutionJobStore(run_dir / "revision-execution-jobs.sqlite")
        try:
            with jobs._conn:
                jobs._conn.execute(
                    "UPDATE revision_execution_job_events SET payload='{}' WHERE job_id=?",
                    (job["job_id"],))
        finally:
            jobs.close()
        with self.assertRaisesRegex(ProtocolError, "队列完整性失败"):
            self.server._recover_revision_execution_jobs()

    def test_reviewed_initial_revision_http_does_not_execute_or_overwrite(self):
        from popper.research.readout import read_research_state
        from popper.workspace_files import ProjectFiles

        self._start(research_names=("research",), trusted_local=True)
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        state = read_research_state(run_dir)
        hypothesis = next(row for row in state["hypotheses"]
                          if row["status"] == "untested"
                          and not row["entity_id"].startswith("H-control-"))
        design = next(row for row in state["designs"]
                      if row["hypothesis_id"] == hypothesis["entity_id"])
        source = ProjectFiles(self.root).read("model.py")
        spec = {"study_id": state["manifest"]["study_id"],
                "expected_state_version": state["state_version"],
                "hypothesis_id": hypothesis["entity_id"],
                "design_id": design["entity_id"], "path": "model.py",
                "expected_sha256": source["sha256"],
                "replacement": source["content"] + "\n# HTTP revision\n"}
        envelope = {"schema_version": "1.0", "spec": spec}
        route = "/api/v1/research/revision-drafts/"
        code, denied = self._post(route + "preview", envelope, token=False)
        self.assertEqual(403, code)
        self.assertEqual("permission_denied", denied["error"]["code"])
        code, preview = self._post(route + "preview", envelope)
        self.assertEqual(200, code)
        self.assertTrue(preview["ok"])
        self.assertIn("+\n", preview["data"]["diff"])
        self.assertEqual([], list((run_dir / "revisions").glob("REV-*")))
        code, stale = self._post(route + "submit", {
            **envelope, "proposal_sha256": "0" * 64})
        self.assertEqual(400, code)
        self.assertFalse(stale["ok"])
        code, created = self._post(route + "submit", {
            **envelope, "proposal_sha256": preview["data"]["proposal_sha256"]})
        self.assertEqual(201, code)
        self.assertEqual("immutable_unexecuted", created["data"]["status"])
        self.assertEqual(source["sha256"], ProjectFiles(self.root).read("model.py")["sha256"])
        after = read_research_state(run_dir)
        self.assertEqual(state["budget"], after["budget"])
        self.assertEqual([], after["observations"])
        self.assertTrue(after["experiment_graph"]["integrity"]["ok"])
        from popper import sandbox
        if sandbox.available():
            execute = {"schema_version": "1.0",
                       "manifest_sha256": created["data"]["manifest_sha256"]}
            revision_id = created["data"]["revision_id"]
            code, denied = self._post(
                f"/api/v1/revisions/{revision_id}/execute", execute, token=False)
            self.assertEqual(403, code)
            code, stale = self._post(
                f"/api/v1/revisions/{revision_id}/execute",
                {**execute, "manifest_sha256": "0" * 64})
            self.assertEqual(400, code)
            self.assertFalse(stale["ok"])
            code, accepted = self._post(
                f"/api/v1/revisions/{revision_id}/execute", execute)
            self.assertEqual(202, code)
            job_id = accepted["data"]["job"]["job_id"]
            self.assertEqual(0, self.server.wait_for_jobs(timeout=120))
            code, finished = self._get(f"/api/v1/revision-jobs/{job_id}")
            self.assertEqual(200, code)
            self.assertEqual("succeeded", finished["data"]["job"]["status"])
            code, listing = self._get("/api/v1/worktrees")
            self.assertEqual(200, code)
            self.assertIn(job_id, {row["job_id"] for row in listing["data"]["jobs"]})
            self.assertFalse((run_dir / "revision-worktrees.sqlite").exists())
            executed = read_research_state(run_dir)
            self.assertEqual(1, len(executed["observations"]))
            self.assertEqual(source["sha256"], ProjectFiles(self.root).read("model.py")["sha256"])
            code, repeated = self._post(
                f"/api/v1/revisions/{revision_id}/execute", execute)
            self.assertEqual(202, code)
            self.assertEqual(job_id, repeated["data"]["job"]["job_id"])

    def _start(self, research_names=(), trusted_local=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self.tmp.name) / "project")
        self.research_dirs = [Path(self.tmp.name) / name for name in research_names]
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  research_runs=self.research_dirs,
                                  trusted_local=trusted_local)
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

    def _post(self, path, payload, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Popper-Token"] = self.token
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

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

    def test_v1_research_uses_stable_envelope_and_trace(self):
        self._start()
        code, denied = self._get("/api/v1/research", token=False)
        self.assertEqual(403, code)
        self.assertFalse(denied["ok"])
        self.assertEqual("permission_denied", denied["error"]["code"])

        code, body = self._get("/api/v1/research")
        self.assertEqual(200, code)
        self.assertTrue(body["ok"])
        self.assertEqual("1.0", body["schema_version"])
        self.assertEqual([], body["data"]["runs"])
        self.assertTrue(body["meta"]["trace_id"].startswith("trc_"))

    def test_frozen_design_execution_job_http_is_durable_and_idempotent(self):
        from popper import sandbox
        if not sandbox.available():
            self.skipTest("sandbox backend unavailable")
        self._start(research_names=("research",), trusted_local=True)
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        controller = ResearchController(run_dir)
        try:
            candidate_id = controller.manifest["candidate_hypothesis_ids"][0]
            original = controller.store.get("design", controller._current_design_id(candidate_id))
            study_id = controller.manifest["study_id"]
            hypothesis_id = "H-http-design"
            design_id = "D-http-design"
            controller.store.add_hypothesis(Hypothesis(
                hypothesis_id=hypothesis_id, study_id=study_id, version=1,
                parent_version=None, mechanism="registered candidate",
                applicability="current development scope", predictions=("effect",),
                falsification=("no effect",)))
            controller.store.create_design(ExperimentDesign(
                design_id=design_id, hypothesis_id=hypothesis_id,
                interventions=tuple(original["interventions"]),
                control=original["control"], baseline=original["baseline"],
                metric=original["metric"], scorer_id=original["scorer_id"],
                metric_direction=original["metric_direction"],
                analysis_unit=original["analysis_unit"],
                splits=tuple(original["splits"]), seeds=tuple(original["seeds"]),
                min_meaningful_effect=original["min_meaningful_effect"]))
            controller.store.freeze_design(design_id)
        finally:
            controller.close()
        detail = self.server.research_design_for_agent(study_id, design_id)
        code, detail_http = self._get(f"/api/v1/designs/{study_id}/{design_id}")
        self.assertEqual(200, code)
        self.assertEqual(detail["design_sha256"], detail_http["data"]["design_sha256"])
        envelope = {"schema_version": "1.0", "study_id": study_id,
                    "design_id": design_id, "expected_version": detail["state_version"],
                    "design_sha256": detail["design_sha256"]}
        code, denied = self._post("/api/v1/design-jobs", envelope, token=False)
        self.assertEqual(403, code)
        code, stale = self._post("/api/v1/design-jobs", {
            **envelope, "expected_version": detail["state_version"] - 1})
        self.assertEqual(400, code)
        self.assertFalse(stale["ok"])
        code, accepted = self._post("/api/v1/design-jobs", envelope)
        self.assertEqual(202, code)
        job_id = accepted["data"]["job"]["job_id"]
        self.assertEqual(0, self.server.wait_for_jobs(timeout=30))
        code, finished = self._get(f"/api/v1/design-jobs/{job_id}")
        self.assertEqual(200, code)
        self.assertEqual("succeeded", finished["data"]["job"]["status"])
        self.assertTrue(finished["data"]["job"]["observation_id"].startswith("OBS-"))
        code, listing = self._get("/api/v1/design-jobs")
        self.assertEqual(200, code)
        self.assertEqual(job_id, listing["data"]["jobs"][0]["job_id"])
        code, repeated = self._post("/api/v1/design-jobs", envelope)
        self.assertEqual(202, code)
        self.assertEqual(job_id, repeated["data"]["job"]["job_id"])

    def test_design_job_recovery_uses_verified_observation_only(self):
        from popper import sandbox
        if not sandbox.available():
            self.skipTest("sandbox backend unavailable")
        self._start(research_names=("research",), trusted_local=True)
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        controller = ResearchController(run_dir)
        try:
            study_id = controller.manifest["study_id"]
            completed_id = controller._current_design_id(
                controller.manifest["candidate_hypothesis_ids"][0])
            missing_id = controller._current_design_id(
                controller.manifest["candidate_hypothesis_ids"][1])
            controller.execute_registered_design(completed_id)
        finally:
            controller.close()
        completed = self.server.research_design_for_agent(study_id, completed_id)
        missing = self.server.research_design_for_agent(study_id, missing_id)
        jobs = DesignExecutionJobStore(run_dir / "design-execution-jobs.sqlite")
        try:
            done_job = jobs.enqueue(completed_id, completed["design_sha256"])
            missing_job = jobs.enqueue(missing_id, missing["design_sha256"])
            jobs.claim(done_job["job_id"])
            jobs.claim(missing_job["job_id"])
        finally:
            jobs.close()
        self.server._recover_design_execution_jobs()
        self.assertEqual("succeeded", self.server.design_job_payload(
            done_job["job_id"])["job"]["status"])
        self.assertEqual("interrupted", self.server.design_job_payload(
            missing_job["job_id"])["job"]["status"])

    def test_workbench_creates_cross_study_followup_from_new_data(self):
        self._start(research_names=("research",), trusted_local=True)
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        study_id = read_json(run_dir / "research.json")["study_id"]
        independent = self.root.parent / "independent-test.json"
        write_json(independent, [
            {"id": f"independent-{i}", "x": 10.0 + i * 0.1,
             "y": 0.5 + 0.7 * (10.0 + i * 0.1) + 1.8 * (10.0 + i * 0.1) ** 2}
            for i in range(30)])
        code, preview = self._get(f"/api/v1/research/{study_id}/followup-preview")
        self.assertEqual(200, code)
        request = {
            "schema_version": "1.0", "parent_study_id": study_id,
            "expected_parent_manifest_sha256": preview["data"]["parent_manifest_sha256"],
            "new_project_name": "followup-project", "new_run_name": "followup-run",
            "new_test_path": str(independent), "candidates": [{"degree": 4}],
            "budget": 1, "objective": "独立数据检验新候选",
        }
        code, denied = self._post("/api/v1/research/followups", request, token=False)
        self.assertEqual(403, code)
        code, stale = self._post("/api/v1/research/followups", {
            **request, "expected_parent_manifest_sha256": "0" * 64})
        self.assertEqual(400, code)
        self.assertFalse(stale["ok"])
        self.assertFalse((self.root.parent / "followup-project").exists())
        code, traversal = self._post("/api/v1/research/followups", {
            **request, "new_project_name": "../escape"})
        self.assertEqual(400, code)
        self.assertFalse(traversal["ok"])
        code, outside = self._post("/api/v1/research/followups", {
            **request, "new_test_path": str(EXAMPLE / "experiment.json")})
        self.assertEqual(400, code)
        self.assertFalse(outside["ok"])
        self.assertFalse((self.root.parent / "followup-project").exists())
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_attempts = list(pool.map(
                lambda _: self._post("/api/v1/research/followups", request), range(2)))
        self.assertTrue(all(code == 201 for code, _ in first_attempts), first_attempts)
        created = next(body for _, body in first_attempts
                       if not body["data"].get("replayed"))
        self.assertEqual(created["data"]["study_id"], next(
            body["data"]["study_id"] for _, body in first_attempts
            if body["data"].get("replayed")))
        self.assertTrue(created["data"]["research_initialized"])
        self.assertNotEqual(study_id, created["data"]["study_id"])
        self.assertEqual(2, len(self.server.research_payload()["runs"]))
        self.assertTrue((self.root.parent / "followup-run" / "research.sqlite").is_file())
        code, repeated = self._post("/api/v1/research/followups", request)
        self.assertEqual(201, code, repeated)
        self.assertTrue(repeated["data"]["replayed"])
        self.assertEqual(created["data"]["study_id"], repeated["data"]["study_id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            concurrent = list(pool.map(
                lambda _: self._post("/api/v1/research/followups", request), range(2)))
        self.assertTrue(all(code == 201 and body["data"]["study_id"] ==
                            created["data"]["study_id"] for code, body in concurrent))
        self.assertEqual(2, len(self.server.research_payload()["runs"]))
        code, conflict = self._post("/api/v1/research/followups", {
            **request, "candidates": [{"degree": 5}]})
        self.assertEqual(400, code)
        self.assertFalse(conflict["ok"])
        self.assertTrue(any(row["root"] == str(self.root.parent / "followup-project")
                            for row in self.server.workspace_payload()["roots"]))
        self.assertTrue((self.root / ".popper" / "research-runs.json").is_file())
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  research_runs=self.research_dirs,
                                  trusted_local=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(2, len(self.server.research_payload()["runs"]))
        self.assertTrue(any(row["root"] == str(self.root.parent / "followup-project")
                            for row in self.server.workspace_payload()["roots"]))
        after_restart = self.server.create_followup_from_workbench(
            study_id, request["new_project_name"], request["new_run_name"],
            request["new_test_path"], request["candidates"], request["budget"],
            request["objective"], request["expected_parent_manifest_sha256"])
        self.assertTrue(after_restart["replayed"])
        self.assertEqual(created["data"]["study_id"], after_restart["study_id"])

    def test_design_job_cancel_http_requires_auth_and_stops_queued_job(self):
        self._start(research_names=("research",), trusted_local=True)
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        jobs = DesignExecutionJobStore(run_dir / "design-execution-jobs.sqlite")
        try:
            job = jobs.enqueue("D-http-cancel", "a" * 64)
        finally:
            jobs.close()
        route = f"/api/v1/design-jobs/{job['job_id']}/cancel"
        payload = {"schema_version": "1.0", "reason": "user requested stop"}
        code, _ = self._post(route, payload, token=False)
        self.assertEqual(403, code)
        code, result = self._post(route, payload)
        self.assertEqual(200, code)
        self.assertEqual("cancelled", result["data"]["job"]["status"])
        self.assertEqual(1, result["data"]["job"]["cancel_requested"])
        code, repeated = self._post(route, payload)
        self.assertEqual(200, code)
        self.assertEqual(result["data"]["job"], repeated["data"]["job"])
        jobs = DesignExecutionJobStore(run_dir / "design-execution-jobs.sqlite")
        try:
            self.assertTrue(jobs.verify()["ok"])
        finally:
            jobs.close()

    def test_design_job_running_cancel_reaches_controller_callback(self):
        self._start(research_names=("research",), trusted_local=True)
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        controller = ResearchController(run_dir)
        try:
            design_id = controller._current_design_id(
                controller.manifest["candidate_hypothesis_ids"][0])
            study_id = controller.manifest["study_id"]
        finally:
            controller.close()
        detail = self.server.research_design_for_agent(study_id, design_id)
        jobs = DesignExecutionJobStore(run_dir / "design-execution-jobs.sqlite")
        try:
            job = jobs.enqueue(design_id, detail["design_sha256"])
        finally:
            jobs.close()
        entered = threading.Event()
        def wait_for_cancel(_controller, _design_id, *, should_cancel, **_kwargs):
            entered.set()
            for _ in range(100):
                if should_cancel():
                    raise RuntimeError("cancel observed")
                threading.Event().wait(0.02)
            raise AssertionError("cancel callback was never triggered")
        with patch.object(ResearchController, "execute_registered_design", wait_for_cancel):
            self.server._start_design_job(run_dir, job["job_id"])
            self.assertTrue(entered.wait(5))
            code, response = self._post(
                f"/api/v1/design-jobs/{job['job_id']}/cancel",
                {"schema_version": "1.0", "reason": "stop running job"})
            self.assertEqual(200, code)
            self.assertEqual("running", response["data"]["job"]["status"])
            self.assertEqual(0, self.server.wait_for_jobs(timeout=10))
        finished = self.server.design_job_payload(job["job_id"])["job"]
        self.assertEqual("cancelled", finished["status"])
        with patch.object(self.server, "_start_design_job") as dispatch:
            retry = {"schema_version": "1.0", "expected_attempt": 1,
                     "design_sha256": detail["design_sha256"]}
            code, response = self._post(
                f"/api/v1/design-jobs/{job['job_id']}/retry", retry)
            self.assertEqual(202, code)
            self.assertEqual("queued", response["data"]["job"]["status"])
            self.assertEqual(2, response["data"]["job"]["attempt"])
            dispatch.assert_called_once()
            code, repeated = self._post(
                f"/api/v1/design-jobs/{job['job_id']}/retry", retry)
            self.assertEqual(202, code)
            self.assertEqual(response["data"]["job"], repeated["data"]["job"])
            dispatch.assert_called_once()
        jobs = DesignExecutionJobStore(run_dir / "design-execution-jobs.sqlite")
        try:
            self.assertTrue(jobs.verify()["ok"])
        finally:
            jobs.close()

    def test_v1_research_command_is_atomic_idempotent_and_guarded(self):
        self._start(research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/v1/research")
        research = state["data"]["runs"][0]
        hypothesis = next(item for item in research["hypotheses"]
                          if not item["hypothesis_id"].startswith("H-control-"))
        command = {
            "schema_version": "1.0",
            "idempotency_key": "research-http-0001",
            "expected_version": research["state_version"],
            "study_id": research["manifest"]["study_id"],
            "action": "set_hypothesis_status",
            "payload": {"hypothesis_id": hypothesis["hypothesis_id"],
                        "version": hypothesis["version"], "status": "retired"},
        }
        code, denied = self._post(
            "/api/v1/commands/research", command, token=False)
        self.assertEqual(403, code)
        self.assertEqual("permission_denied", denied["error"]["code"])

        code, first = self._post("/api/v1/commands/research", command)
        self.assertEqual(200, code)
        self.assertEqual("retired", first["data"]["status"])
        self.assertGreater(first["meta"]["event_cursor"], 0)
        store = ResearchStore(run_dir / "research.sqlite")
        try:
            receipt = store.command_receipt("research-http-0001")
        finally:
            store.close()
        self.assertEqual("local_user", receipt["actor"])
        self.assertEqual(first["meta"]["trace_id"], receipt["trace_id"])
        code, replay = self._post("/api/v1/commands/research", command)
        self.assertEqual(200, code)
        self.assertEqual(first["data"], replay["data"])

        _, current = self._get("/api/v1/research")
        updated = next(item for item in current["data"]["runs"][0]["hypotheses"]
                       if item["hypothesis_id"] == hypothesis["hypothesis_id"])
        self.assertEqual("retired", updated["status"])

        stale = dict(command)
        stale["idempotency_key"] = "research-http-0002"
        stale["payload"] = {**command["payload"], "status": "inconclusive"}
        code, conflict = self._post("/api/v1/commands/research", stale)
        self.assertEqual(409, code)
        self.assertEqual("version_conflict", conflict["error"]["code"])

        forbidden = dict(command)
        forbidden["idempotency_key"] = "research-http-0003"
        forbidden["expected_version"] = first["data"]["state_version"]
        forbidden["action"] = "add_observation"
        forbidden["payload"] = {}
        code, rejected = self._post("/api/v1/commands/research", forbidden)
        self.assertEqual(400, code)
        self.assertEqual("invalid_request", rejected["error"]["code"])

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
        self.assertEqual({"kind": "study", "id": run["manifest"]["study_id"]},
                         run["manifest"]["study_ref"])
        self.assertTrue(all(h["entity_ref"]["kind"] == "hypothesis"
                            for h in run["hypotheses"]))
        self.assertEqual(4, len(run["designs"]))
        self.assertTrue(all(d["entity_ref"]["kind"] == "design"
                            for d in run["designs"]))
        self.assertEqual([], run["runs"])
        self.assertTrue(run["experiment_graph"]["integrity"]["ok"])
        self.assertEqual([], run["experiment_graph"]["integrity"]["issues"])
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

    def test_revision_endpoint_returns_verified_identity_bound_diff(self):
        self._start(research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        controller = ResearchController(run_dir)
        try:
            hypothesis = next(row for row in controller.status()["candidates"]
                              if not row["hypothesis_id"].startswith("H-control-"))
            source = self.root / "model.py"
            revision = controller.revisions.create(
                self.root, hypothesis["hypothesis_id"], hypothesis["design_id"],
                [CodeEdit("model.py", source.read_text(encoding="utf-8") + "\n# api diff\n",
                          file_hash(source))], "http-test")
        finally:
            controller.close()
        revision_id = revision["revision_id"]

        code, denied = self._get(f"/api/v1/revisions/{revision_id}", token=False)
        self.assertEqual(403, code)
        self.assertEqual("permission_denied", denied["error"]["code"])
        code, body = self._get(f"/api/v1/revisions/{revision_id}")
        self.assertEqual(200, code)
        detail = body["data"]
        self.assertEqual(revision_id, detail["revision_id"])
        self.assertEqual(hypothesis["design_id"], detail["design_id"])
        self.assertIn("--- a/model.py", detail["diff"])
        self.assertIn("+# api diff", detail["diff"])
        self.assertEqual(revision["identity"]["diff_sha256"], detail["diff_sha256"])
        self.assertEqual("frozen_identity", detail["diff_source"])
        self.assertIsNone(detail["diff_attestation_id"])
        self.assertEqual([str(run_dir.resolve())], detail["research_runs"])

        code, missing = self._get("/api/v1/revisions/REV-" + "0" * 24)
        self.assertEqual(404, code)
        self.assertEqual("not_found", missing["error"]["code"])

    def test_trusted_api_migrates_legacy_child_diff_without_rewriting_manifest(self):
        self._start(research_names=("research",), trusted_local=True)
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        controller = ResearchController(run_dir)
        try:
            hypothesis = next(row for row in controller.status()["candidates"]
                              if not row["hypothesis_id"].startswith("H-control-"))
            source = self.root / "model.py"
            parent = controller.revisions.create(
                self.root, hypothesis["hypothesis_id"], hypothesis["design_id"],
                [CodeEdit("model.py", source.read_text(encoding="utf-8") + "\n# parent\n",
                          file_hash(source))], "http-test")
            parent_source = controller.revisions.path(parent["revision_id"]) / "code/model.py"
            child = controller.revisions.create(
                self.root, hypothesis["hypothesis_id"], hypothesis["design_id"],
                [CodeEdit("model.py", parent_source.read_text(encoding="utf-8") + "\n# legacy\n",
                          file_hash(parent_source))], "http-test", parent["revision_id"])
            old_dir = controller.revisions.path(child["revision_id"])
            manifest = read_json(old_dir / "revision.json")
            manifest["identity"].pop("diff_sha256")
            manifest.pop("diff")
            legacy_id = "REV-" + digest(manifest["identity"])[:24]
            manifest["revision_id"] = legacy_id
            write_json(old_dir / "revision.json", manifest)
            old_dir.rename(run_dir / "revisions" / legacy_id)
        finally:
            controller.close()
        manifest_path = run_dir / "revisions" / legacy_id / "revision.json"
        before = file_hash(manifest_path)
        code, body = self._post(
            f"/api/v1/revisions/{legacy_id}/migrate-diff", {"schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("migrated_attestation", body["data"]["diff_source"])
        self.assertTrue(body["data"]["diff_attestation_id"].startswith("DFA-"))
        self.assertIn("+# legacy", body["data"]["diff"])
        self.assertEqual(before, file_hash(manifest_path))

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_trusted_worktree_api_is_idempotent_persistent_and_removable(self):
        self._start(research_names=("research",), trusted_local=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "popper@example.invalid"],
                       cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Popper Test"],
                       cwd=self.root, check=True)
        subprocess.run(["git", "add", "model.py"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "source"], cwd=self.root, check=True)
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        controller = ResearchController(run_dir)
        try:
            hypothesis = next(row for row in controller.status()["candidates"]
                              if not row["hypothesis_id"].startswith("H-control-"))
            source = self.root / "model.py"
            revision = controller.revisions.create(
                self.root, hypothesis["hypothesis_id"], hypothesis["design_id"],
                [CodeEdit("model.py", source.read_text(encoding="utf-8") + "\n# worktree\n",
                          file_hash(source))], "http-test")
        finally:
            controller.close()
        payload = {"schema_version": "1.0", "idempotency_key": "http-worktree-0001"}
        path = f"/api/v1/revisions/{revision['revision_id']}/worktrees"
        code, first = self._post(path, payload)
        self.assertEqual(201, code)
        record = first["data"]
        self.assertEqual("active", record["status"])
        self.assertTrue(Path(record["path"]).is_dir())
        code, replay = self._post(path, payload)
        self.assertEqual(201, code)
        self.assertEqual(record, replay["data"])

        code, listing = self._get("/api/v1/worktrees")
        self.assertEqual(200, code)
        self.assertEqual([record["worktree_id"]],
                         [row["worktree_id"] for row in listing["data"]["worktrees"]])
        worktree_file = Path(record["path"]) / "model.py"
        approved = worktree_file.read_text(encoding="utf-8").replace(
            "weights = solve(matrix, rhs)", "weights = list(solve(matrix, rhs))")
        worktree_file.write_text(approved, encoding="utf-8")
        code, proposal = self._post(
            f"/api/v1/worktrees/{record['worktree_id']}/proposal",
            {"schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("review_required", proposal["data"]["status"])
        self.assertNotIn("proposal", proposal["data"])
        proposal_sha = proposal["data"]["proposal_sha256"]
        worktree_file.write_text("raise RuntimeError('late edit')\n", encoding="utf-8")
        code, submitted = self._post(
            f"/api/v1/worktrees/{record['worktree_id']}/submit",
            {"schema_version": "1.0", "proposal_sha256": proposal_sha})
        self.assertEqual(200, code)
        self.assertEqual("submitted", submitted["data"]["status"])
        successor_id = submitted["data"]["successor_revision_id"]
        successor = RevisionStore(run_dir / "revisions").verify(successor_id)
        self.assertEqual(revision["revision_id"], successor["identity"]["parent_revision_id"])
        self.assertEqual(approved, (run_dir / "revisions" / successor_id
                                    / "code/model.py").read_text(encoding="utf-8"))
        code, execution = self._post(
            f"/api/v1/worktrees/{record['worktree_id']}/execute",
            {"schema_version": "1.0"})
        self.assertEqual(202, code)
        job_id = execution["data"]["job"]["job_id"]
        self.assertEqual(0, self.server.wait_for_jobs(timeout=60))
        code, execution = self._get(f"/api/v1/revision-jobs/{job_id}")
        self.assertEqual(200, code)
        self.assertEqual("succeeded", execution["data"]["job"]["status"])
        self.assertEqual(100, execution["data"]["job"]["progress"])
        self.assertTrue(execution["data"]["integrity"]["ok"])
        code, listing = self._get("/api/v1/worktrees")
        final_worktree = next(row for row in listing["data"]["worktrees"]
                              if row["worktree_id"] == record["worktree_id"])
        self.assertEqual("executed", final_worktree["status"])
        self.assertTrue(final_worktree["execution_run_id"])
        code, replay_execution = self._post(
            f"/api/v1/worktrees/{record['worktree_id']}/execute",
            {"schema_version": "1.0"})
        self.assertEqual(202, code)
        self.assertEqual(job_id, replay_execution["data"]["job"]["job_id"])
        self.assertEqual("succeeded", replay_execution["data"]["job"]["status"])
        code, audit = self._get(
            f"/api/v1/worktrees/{record['worktree_id']}/events")
        self.assertEqual(200, code)
        self.assertTrue(audit["data"]["integrity"]["ok"])
        self.assertEqual(
            ["provisioning", "activated", "proposal_frozen", "submitted",
             "execution_started", "executed"],
            [event["event_type"] for event in audit["data"]["events"]])
        code, removed = self._post(
            f"/api/v1/worktrees/{record['worktree_id']}/remove",
            {"schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("removed", removed["data"]["status"])
        self.assertFalse(Path(record["path"]).exists())

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
        self.assertTrue(all(o["entity_ref"]["kind"] == "observation"
                            for o in run["observations"]))
        self.assertTrue(all(d["entity_ref"]["kind"] == "decision"
                            for d in run["decisions"]))
        self.assertTrue(run["runs"])
        self.assertTrue(all(r["entity_ref"]["kind"] == "run" for r in run["runs"]))
        self.assertTrue(all(r.get("revision_id") and r.get("revision_manifest_sha256")
                            for r in run["runs"]))
        graph = run["experiment_graph"]
        self.assertTrue(graph["integrity"]["ok"], graph["integrity"]["issues"])
        relations = {edge["relation"] for edge in graph["edges"]}
        self.assertTrue({"designed_as", "implemented_by", "executed_as",
                         "evaluated_as", "informed"}.issubset(relations))
        self.assertTrue(any(node["kind"] == "code_revision" for node in graph["nodes"]))
        allowed = set(body["status_labels"]["action"])
        self.assertTrue(all(d["action"] in allowed for d in run["decisions"]))
        self.assertEqual(before, self._db_fingerprint(run_dir))

        # Artifact tampering is reported by the graph projection and never
        # repaired or hidden during this read-only request.
        revision_file = next((run_dir / "revisions").glob("REV-*/code/*.py"))
        revision_file.write_text(revision_file.read_text(encoding="utf-8") + "\n# tampered\n",
                                 encoding="utf-8")
        code, changed = self._get("/api/research")
        self.assertEqual(200, code)
        broken = changed["runs"][0]["experiment_graph"]["integrity"]
        self.assertFalse(broken["ok"])
        self.assertTrue(any(issue["code"] == "revision_integrity_failed"
                            for issue in broken["issues"]))
        self.assertEqual(before, self._db_fingerprint(run_dir))


if __name__ == "__main__":
    unittest.main()
