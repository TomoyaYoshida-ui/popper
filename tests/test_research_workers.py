import os
import runpy
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from popper.core import ProtocolError, file_hash, initialize, read_json, write_json
from popper.research.revisions import CodeEdit, RevisionStore
from popper.research.workers import (
    InputArtifact, JobSpec, LocalWorker, RemoteWorker, TERMINAL_JOB_STATUSES,
    job_invocation, staged_input_artifacts)
from popper.research.workers.supervisor import supervise_job
from popper.research.controller import ResearchController, RevisionExecutionError
from popper.research.actions import ActionProposal, RUN_EXPERIMENT
from popper.research.models import EvidenceDrivenPolicy
from popper.research.contracts import HypothesisStatus, StudyStatus
from popper import sandbox


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class ResearchWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.project / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.project)
        initialize(self.project)
        self.revisions = RevisionStore(root / "revisions")
        source = self.project / "model.py"
        self.manifest = self.revisions.create(
            self.project, "H1", "D1",
            [CodeEdit("model.py", source.read_text(encoding="utf-8") + "\n# revision\n",
                      file_hash(source))], "test-policy")
        self.revision_dir = self.revisions.path(self.manifest["revision_id"])

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, key="same-key", args=()):
        return JobSpec(
            idempotency_key=key,
            revision_id=self.manifest["revision_id"],
            revision_manifest_sha256=file_hash(self.revision_dir / "revision.json"),
            design_id="D1", entrypoint="model.py", args=tuple(args),
            outputs=("outputs/result.json",), timeout_seconds=10)

    def test_revision_is_content_addressed_and_tamper_evident(self):
        source = self.project / "model.py"
        edit = CodeEdit("model.py", source.read_text(encoding="utf-8") + "\n# revision\n",
                        file_hash(source))
        repeated = self.revisions.create(self.project, "H1", "D1", [edit], "test-policy")
        self.assertEqual(self.manifest["revision_id"], repeated["revision_id"])
        target = self.revision_dir / "code" / "model.py"
        target.write_text("# tampered", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "已变化"):
            self.revisions.verify(self.manifest["revision_id"])

    def test_revision_requires_exact_source_hash(self):
        with self.assertRaisesRegex(ProtocolError, "SHA-256"):
            self.revisions.create(
                self.project, "H2", "D2", [CodeEdit("model.py", "x = 1\n", "0" * 64)],
                "test-policy")

    def test_repair_revision_is_linked_to_immutable_parent(self):
        parent_file = self.revision_dir / "code/model.py"
        child = self.revisions.create(
            self.project, "H1", "D1",
            [CodeEdit("model.py", parent_file.read_text(encoding="utf-8") + "\n# repair\n",
                      file_hash(parent_file))], "repair-policy",
            parent_revision_id=self.manifest["revision_id"])
        self.assertEqual(self.manifest["revision_id"],
                         child["identity"]["parent_revision_id"])
        self.assertNotEqual(self.manifest["revision_id"], child["revision_id"])

    def test_repair_restoring_original_has_no_cumulative_executable_change(self):
        parent_file = self.revision_dir / "code/model.py"
        child = self.revisions.create(self.project, "H1", "D1", [CodeEdit(
            "model.py", (self.project / "model.py").read_text(encoding="utf-8"),
            file_hash(parent_file))], "repair-policy",
            parent_revision_id=self.manifest["revision_id"])
        self.assertFalse(any(child["identity"]["execution_changes"].values()))

    def test_probe_preserves_nested_entrypoint_imports(self):
        edits = [CodeEdit("nested/main.py",
                         "from pathlib import Path\nfrom helper import predict\n"
                         "Path('outputs/result.json').write_text(str(predict()))\n"),
                 CodeEdit("nested/helper.py", "def predict():\n    return 42\n")]
        revision = self.revisions.create(self.project, "H-nested", "D-nested", edits,
                                         "nested-policy")
        def executor(command, workspace, env, stdout, stderr, spec):
            subprocess.run(command, cwd=workspace, env=env, stdout=stdout,
                           stderr=stderr, timeout=spec.timeout_seconds, check=True)
        worker = LocalWorker(Path(self.temp.name) / "nested-jobs", self.revisions, executor)
        spec = JobSpec("nested", revision["revision_id"],
                       file_hash(self.revisions.path(revision["revision_id"]) / "revision.json"),
                       "D-nested", "nested/main.py", outputs=("outputs/result.json",),
                       require_edit_coverage=True)
        receipt = worker.run(spec)
        self.assertEqual("succeeded", receipt["status"])
        self.assertTrue(receipt["execution_gate"]["passed"])

    def test_windows_code_edit_paths_are_normalized(self):
        """两侧都必须把 `nested\\helper.py` 归一成同一个 POSIX 形式。

        这条原先挂在 `skipUnless(os.name == 'nt')` 上，而 Linux 上同一份提案会被当成
        一个含反斜杠的文件名——正是「换台机器跑出不同产物」的形状，所以守卫拿掉。
        """
        edit = CodeEdit("nested\\helper.py", "value = 1\n")
        self.assertEqual("nested/helper.py", edit.path)

    def test_job_is_idempotent_and_receipt_is_tamper_evident(self):
        calls = []

        def executor(command, workspace, env, stdout, stderr, spec):
            calls.append(command)
            output = workspace / "outputs" / "result.json"
            output.parent.mkdir(exist_ok=True)
            output.write_text('{"ok":true}', encoding="utf-8")

        worker = LocalWorker(Path(self.temp.name) / "jobs", self.revisions, executor)
        first = worker.run(self.spec())
        second = worker.run(self.spec())
        self.assertEqual("succeeded", first["status"])
        self.assertEqual(first, second)
        self.assertEqual(1, len(calls))
        worker.collect(self.spec().job_id)
        result = Path(self.temp.name) / "jobs" / self.spec().job_id / "workspace/outputs/result.json"
        result.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "已变化"):
            worker.collect(self.spec().job_id)
        with self.assertRaisesRegex(ProtocolError, "已变化"):
            worker.run(self.spec())

    def test_idempotency_key_cannot_name_a_different_job(self):
        worker = LocalWorker(Path(self.temp.name) / "jobs", self.revisions,
                             lambda *unused: None)
        worker.submit(self.spec(args=("one",)))
        with self.assertRaisesRegex(ProtocolError, "不同 JobSpec"):
            worker.submit(self.spec(args=("two",)))

    def test_failure_logs_are_bounded_and_tamper_checked(self):
        def executor(command, workspace, env, stdout, stderr, spec):
            stderr.write(b"x" * 10000 + b"RuntimeError: broken")
            raise RuntimeError("broken")

        worker = LocalWorker(Path(self.temp.name) / "logs", self.revisions, executor)
        spec = self.spec("failure-logs")
        worker.run(spec)
        context = worker.failure_context(spec.job_id, max_bytes=100)
        log = context["logs"]["stderr.log"]
        self.assertTrue(log["truncated"])
        self.assertEqual(100, len(log["text"]))
        self.assertTrue(log["text"].endswith("RuntimeError: broken"))
        self.assertEqual("untrusted_execution_output", context["trust"])
        (worker.root / spec.job_id / "stderr.log").write_text("forged")
        with self.assertRaisesRegex(ProtocolError, "已变化"):
            worker.failure_context(spec.job_id)

    def test_generated_code_never_falls_back_to_trusted_local(self):
        from unittest.mock import patch
        worker = LocalWorker(Path(self.temp.name) / "jobs", self.revisions)
        with patch("popper.research.workers.local.sandbox.available", return_value=False):
            receipt = worker.run(self.spec("no-sandbox"))
        self.assertEqual("infrastructure_failed", receipt["status"])
        self.assertEqual("OSError", receipt["error_type"])

    def test_worker_cache_environment_is_private_writable_and_does_not_inherit_secrets(self):
        cache_names = ("TORCHINDUCTOR_CACHE_DIR", "TORCH_HOME", "TORCH_EXTENSIONS_DIR",
                       "TRITON_CACHE_DIR", "CUDA_CACHE_PATH", "XDG_CACHE_HOME",
                       "TORCH_COMPILE_DEBUG_DIR")
        source = (
            "import getpass, json, os\nfrom pathlib import Path\n"
            f"cache_names = {cache_names!r}\n"
            "assert getpass.getuser() == 'popper-worker'\n"
            "for name in ('HOME', 'USERPROFILE', 'LOGNAME', 'USER', 'LNAME', 'DEEPSEEK_API_KEY'):\n"
            "    assert name not in os.environ, 'Inherited private parent environment'\n"
            "temporary = (Path.cwd() / 'tmp').resolve()\n"
            "assert Path(os.environ['TEMP']).resolve() == temporary\n"
            "assert Path(os.environ['TMP']).resolve() == temporary\n"
            "paths = {}\n"
            "for name in cache_names:\n"
            "    directory = Path(os.environ[name]).resolve()\n"
            "    assert directory.is_relative_to(temporary), 'Cache escaped job tmp'\n"
            "    directory.mkdir(parents=True, exist_ok=True)\n"
            "    marker = directory / 'cache-marker.txt'\n"
            "    assert not marker.exists(), 'Cache leaked between jobs'\n"
            "    marker.write_text('worker-created', encoding='utf-8')\n"
            "    paths[name] = str(directory)\n"
            "Path('outputs/result.json').write_text(json.dumps(paths), encoding='utf-8')\n"
        )
        revision = self.revisions.create(self.project, "H-env", "D-env",
                                         [CodeEdit("runner.py", source)], "test-policy")

        def executor(command, workspace, env, stdout, stderr, spec):
            subprocess.run(command, cwd=workspace, env=env, stdout=stdout,
                           stderr=stderr, timeout=spec.timeout_seconds, check=True)

        worker = LocalWorker(Path(self.temp.name) / "environment-jobs", self.revisions, executor)
        inherited = {name: str(Path(self.temp.name) / "parent-cache") for name in cache_names}
        inherited.update({"HOME": "parent-private-home", "USERPROFILE": "parent-private-profile",
                          "USERNAME": "parent-private-user", "LOGNAME": "parent-private-user",
                          "USER": "parent-private-user", "LNAME": "parent-private-user",
                          "DEEPSEEK_API_KEY": "synthetic-test-secret"})
        results = []
        with patch.dict("os.environ", inherited):
            for key in ("environment-first", "environment-second"):
                spec = JobSpec(key, revision["revision_id"], file_hash(
                    self.revisions.path(revision["revision_id"]) / "revision.json"),
                    "D-env", "runner.py", outputs=("outputs/result.json",))
                receipt = worker.run(spec)
                self.assertEqual("succeeded", receipt["status"])
                worker.collect(spec.job_id)
                results.append(read_json(worker.root / spec.job_id / "workspace/outputs/result.json"))
        self.assertTrue(set(results[0].values()).isdisjoint(results[1].values()))

    def test_code_copy_mutation_is_an_implementation_failure(self):
        def mutating_executor(command, workspace, env, stdout, stderr, spec):
            (workspace / "code/model.py").write_text("# mutation", encoding="utf-8")
            output = workspace / "outputs/result.json"
            output.parent.mkdir(exist_ok=True)
            output.write_text("{}", encoding="utf-8")

        worker = LocalWorker(Path(self.temp.name) / "jobs", self.revisions, mutating_executor)
        receipt = worker.run(self.spec("mutator"))
        self.assertEqual("implementation_failed", receipt["status"])
        self.assertEqual("ProtocolError", receipt["error_type"])

    @unittest.skipUnless(sandbox.available(), "本平台没有可用的沙箱后端")
    def test_real_os_sandbox_can_write_only_declared_output_root(self):
        source = "from pathlib import Path\nPath('outputs/result.json').write_text('ok')\n"
        revision = self.revisions.create(
            self.project, "H-os", "D-os", [CodeEdit("runner.py", source)], "test-policy")
        revision_dir = self.revisions.path(revision["revision_id"])
        spec = JobSpec(
            "real-os-sandbox", revision["revision_id"],
            file_hash(revision_dir / "revision.json"), "D-os", "runner.py",
            outputs=("outputs/result.json",), timeout_seconds=10)
        worker = LocalWorker(Path(self.temp.name) / "os-jobs", self.revisions)
        receipt = worker.run(spec)
        self.assertEqual("succeeded", receipt["status"])
        # 后端名跟着真实选中的内核机制走（Windows 低完整性 / Linux bubblewrap），
        # 不是写死的 Windows 字符串。
        self.assertEqual(sandbox.execution_backend_name(), receipt["execution_backend"])
        self.assertIn(receipt["execution_backend"],
                      ("windows_low_integrity", "linux_bubblewrap"))


class SupervisorLifecycleTests(unittest.TestCase):
    """分离式 supervisor 的生命周期：重连判活、取消、以及 cancel 终态语义。

    这些用例直接覆盖「控制器崩溃→重连」与「原地取消」两条关键路径，不依赖
    OS 低完整性后端是否可用：心跳判活与取消标记都是纯文件契约。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.project / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.project)
        initialize(self.project)
        self.revisions = RevisionStore(root / "revisions")
        source = self.project / "model.py"
        self.manifest = self.revisions.create(
            self.project, "H1", "D1",
            [CodeEdit("model.py", source.read_text(encoding="utf-8") + "\n# revision\n",
                      file_hash(source))], "test-policy")
        self.revision_dir = self.revisions.path(self.manifest["revision_id"])

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, key="supervisor-key"):
        return JobSpec(
            idempotency_key=key, revision_id=self.manifest["revision_id"],
            revision_manifest_sha256=file_hash(self.revision_dir / "revision.json"),
            design_id="D1", entrypoint="model.py", args=(),
            outputs=("outputs/result.json",), timeout_seconds=10)

    def worker(self, name="jobs"):
        return LocalWorker(Path(self.temp.name) / name, self.revisions)

    def _mark_running(self, worker, job_id, heartbeat_age):
        manifest = read_json(worker.root / job_id / "job.json")
        manifest["status"] = "running"
        write_json(worker.root / job_id / "job.json", manifest)
        write_json(worker.root / job_id / "supervisor.json",
                   {"pid": 999999, "heartbeat": time.time() - heartbeat_age})

    def test_reap_stale_flags_dead_supervisor_as_infrastructure_failure(self):
        worker = self.worker()
        spec = self.spec("stale-supervisor")
        worker.submit(spec)
        self._mark_running(worker, spec.job_id, heartbeat_age=3600)
        worker.reap_stale([spec.job_id])
        self.assertEqual("infrastructure_failed", worker.poll(spec.job_id)["status"])
        receipt = worker.collect(spec.job_id)
        self.assertEqual("SupervisorLost", receipt["error_type"])

    def test_reap_stale_keeps_live_supervisor_running(self):
        worker = self.worker()
        spec = self.spec("live-supervisor")
        worker.submit(spec)
        self._mark_running(worker, spec.job_id, heartbeat_age=0)
        worker.reap_stale([spec.job_id])
        self.assertEqual("running", worker.poll(spec.job_id)["status"])

    def test_cancel_queued_job_finalizes_cancelled_immediately(self):
        worker = self.worker()
        spec = self.spec("cancel-queued")
        worker.submit(spec)
        manifest = worker.cancel(spec.job_id)
        self.assertEqual("cancelled", manifest["status"])
        receipt = worker.collect(spec.job_id)
        self.assertEqual("cancelled", receipt["status"])
        self.assertEqual("Cancelled", receipt["error_type"])

    def test_supervisor_finalizes_pre_cancelled_job_without_executing(self):
        worker = self.worker()
        spec = self.spec("pre-cancelled")
        worker.submit(spec)
        self._mark_running(worker, spec.job_id, heartbeat_age=0)
        write_json(worker.root / spec.job_id / "cancel_requested",
                   {"requested_at": time.time()})
        result = supervise_job(worker.root, spec.job_id, str(self.revisions.root))
        self.assertEqual("cancelled", result["status"])
        self.assertEqual("cancelled", worker.poll(spec.job_id)["status"])

    def test_supervise_job_does_not_rerun_terminal_job(self):
        worker = self.worker()
        spec = self.spec("terminal-reconnect")
        worker.submit(spec)
        manifest = read_json(worker.root / spec.job_id / "job.json")
        worker._finalize_manual(worker.root / spec.job_id, manifest, "succeeded", None)
        result = supervise_job(worker.root, spec.job_id, str(self.revisions.root))
        self.assertEqual("succeeded", result["status"])
        # 重连后 job 应停留于终态，且回执可被 collect 校验。
        self.assertIn(worker.poll(spec.job_id)["status"], TERMINAL_JOB_STATUSES)
        self.assertEqual("succeeded", worker.collect(spec.job_id)["status"])

    RESULT = '{"ok":true}\n'

    def _write_result(self, workspace):
        output = Path(workspace) / "outputs" / "result.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(self.RESULT, encoding="utf-8")

    def test_supervise_job_drives_queued_to_succeeded(self):
        worker = self.worker("supervise-jobs")
        spec = self.spec("supervise-succeeded")
        worker.submit(spec)

        def fake_run(command, cwd, env, stdout, stderr, timeout_seconds,
                     mem_limit_mb=None, cpu_time_seconds=None, max_processes=None,
                     sandboxed=False, on_spawn=None):
            if on_spawn is not None:
                on_spawn(Mock(pid=12345))
            self._write_result(cwd)

        with patch("popper.sandbox.available", return_value=True), \
                patch("popper.sandbox.label_write_scope"), \
                patch("popper.sandbox.unlabel_write_scope"), \
                patch("popper.sandbox.launch", side_effect=fake_run):
            result = supervise_job(worker.root, spec.job_id, str(self.revisions.root))
        self.assertEqual("succeeded", result["status"])
        self.assertEqual("succeeded", worker.poll(spec.job_id)["status"])
        self.assertEqual("succeeded", worker.collect(spec.job_id)["status"])

    def test_supervise_and_sync_paths_are_byte_identical(self):
        sync_worker = LocalWorker(
            Path(self.temp.name) / "sync-jobs", self.revisions,
            lambda command, workspace, env, stdout, stderr, spec: self._write_result(workspace))
        sync_receipt = sync_worker.run(self.spec("byte-sync"))

        worker = self.worker("supervise-jobs")
        spec = self.spec("byte-supervise")
        worker.submit(spec)

        def fake_run(command, cwd, env, stdout, stderr, timeout_seconds,
                     mem_limit_mb=None, cpu_time_seconds=None, max_processes=None,
                     sandboxed=False, on_spawn=None):
            if on_spawn is not None:
                on_spawn(Mock(pid=12345))
            self._write_result(cwd)

        with patch("popper.sandbox.available", return_value=True), \
                patch("popper.sandbox.label_write_scope"), \
                patch("popper.sandbox.unlabel_write_scope"), \
                patch("popper.sandbox.launch", side_effect=fake_run):
            result = supervise_job(worker.root, spec.job_id, str(self.revisions.root))
        self.assertEqual("succeeded", result["status"])
        supervise_receipt = worker.collect(spec.job_id)
        self.assertEqual(sync_receipt["artifacts"]["outputs/result.json"],
                         supervise_receipt["artifacts"]["outputs/result.json"])

    def test_cancel_terminates_running_job_and_marks_cancelled(self):
        worker = self.worker("cancel-jobs")
        spec = self.spec("cancel-running")
        worker.submit(spec)
        job_dir = worker.root / spec.job_id
        running = threading.Event()
        killed = threading.Event()

        def fake_run(command, cwd, env, stdout, stderr, timeout_seconds,
                     mem_limit_mb=None, cpu_time_seconds=None, max_processes=None,
                     sandboxed=False, on_spawn=None):
            if on_spawn is not None:
                on_spawn(Mock(pid=12345))
            running.set()
            killed.wait(10)  # 模拟长时间 job，直到 terminate_tree 触发
            raise OSError("terminated")

        def fake_terminate(pid):
            killed.set()

        with patch("popper.sandbox.available", return_value=True), \
                patch("popper.sandbox.label_write_scope"), \
                patch("popper.sandbox.unlabel_write_scope"), \
                patch("popper.sandbox.launch", side_effect=fake_run), \
                patch("popper.sandbox.terminate_tree", side_effect=fake_terminate) as terminate_mock:
            thread = threading.Thread(
                target=supervise_job,
                args=(worker.root, spec.job_id, str(self.revisions.root)))
            thread.start()
            self.assertTrue(running.wait(5))
            write_json(job_dir / "cancel_requested", {"requested_at": time.time()})
            thread.join(15)
        self.assertFalse(thread.is_alive())
        terminate_mock.assert_called_once()
        self.assertEqual("cancelled", worker.poll(spec.job_id)["status"])
        receipt = worker.collect(spec.job_id)
        self.assertEqual("cancelled", receipt["status"])
        self.assertEqual("Cancelled", receipt["error_type"])


class RevisionControllerIntegrationTests(unittest.TestCase):
    def test_repair_is_bounded_and_does_not_retry_other_failures(self):
        for error, available, expected_calls in (
            (RevisionExecutionError("REV-test", "implementation_failed", "RuntimeError"), 2, 2),
            (RevisionExecutionError("REV-test", "implementation_failed", "RuntimeError"), 0, 1),
            (RevisionExecutionError("REV-test", "infrastructure_failed", "TimeoutExpired"), 2, 1),
            (ProtocolError("integrity failure"), 2, 1),
        ):
            with self.subTest(error=str(error), available=available):
                controller = object.__new__(ResearchController)
                controller.manifest = {"study_id": "S-test"}
                controller.store = Mock()
                controller.implement = Mock(side_effect=error)
                with patch("popper.research.controller.build_context", return_value={
                        "budget": {"available": available}}):
                    with self.assertRaises(ProtocolError):
                        controller._implement_with_repair("H-test")
                self.assertEqual(expected_calls, controller.implement.call_count)
                if expected_calls == 2:
                    controller.implement.assert_called_with(
                        "H-test", parent_revision_id="REV-test")

    def test_revision_execution_becomes_a_scored_observation(self):
        self._exercise_revision(False)

    def test_failed_revision_is_repaired_and_independently_scored(self):
        self._exercise_revision(True)

    def test_unused_helper_is_blocked_then_wired_in_by_repair(self):
        self._exercise_revision(True, unused_helper=True)

    def _exercise_revision(self, fail_first, unused_helper=False):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project, run_dir = root / "project", root / "research"
            project.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(EXAMPLE / name, project / name)
            runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](project)
            initialize(project)

            class Policy(EvidenceDrivenPolicy):
                name = "test_code_policy"
                model = "test-model"
                calls = []

                def propose_revision(self, objective, hypothesis, config, code_files,
                                     parent_revision=None, failure=None):
                    source = next(row for row in code_files if row["path"] == "model.py")
                    self.calls.append((parent_revision, failure))
                    if unused_helper and parent_revision is None:
                        return {"edits": (CodeEdit("helper.py",
                            "def transform(values):\n    return [float(v) for v in values]\n"),),
                            "rationale": "unused helper regression"}
                    content = source["content"]
                    if unused_helper:
                        content = content.replace("weights = solve(matrix, rhs)",
                            "from helper import transform\n    weights = transform(solve(matrix, rhs))")
                    elif fail_first and parent_revision is None:
                        content = "raise RuntimeError('injected implementation failure')\n" + content
                    elif fail_first:
                        content = content.split("\n", 1)[1]
                    content = content.replace("weights = solve(matrix, rhs)",
                                              "weights = list(solve(matrix, rhs))")
                    return {"edits": (CodeEdit(
                        source["path"], content + "\n# generated revision\n",
                        source["sha256"]),), "rationale": "实现冻结配置的候选版本。"}

            policy = Policy()
            ResearchController.initialize(project, run_dir, policy=policy)
            controller = ResearchController(run_dir, policy=policy)

            def executor(command, workspace, env, stdout, stderr, spec):
                subprocess.run(command, cwd=workspace, env=env, stdout=stdout,
                               stderr=stderr, timeout=spec.timeout_seconds, check=True)

            controller.worker.executor = executor
            try:
                # C1：confirm 路径需要 baseline + candidate 都在 dev 上跑过，
                # 否则 freeze 会以「需要成功的基线和至少一个候选」失败。
                # _execute_development 走 _evaluate_registered_via_worker，
                # 与 candidate 的 implement 路径共用同一 Worker（executor 注入）。
                control_id = controller.manifest["control_hypothesis_id"]
                controller._execute_development(
                    control_id,
                    ActionProposal(RUN_EXPERIMENT, "test baseline", control_id,
                                   source="test"),
                    trusted_local=False, sandboxed=True)
                hypothesis_id = controller.status()["candidates"][0]["hypothesis_id"]
                result = controller._implement_with_repair(hypothesis_id)
                self.assertEqual("succeeded", result["status"])
                self.assertEqual("dev", result["observation"]["scope"])
                self.assertEqual(3, len(result["receipts"]))
                self.assertTrue(all(r["status"] == "succeeded" for r in result["receipts"]))
                status = controller.status()
                self.assertEqual(2 if fail_first else 1, status["code_revisions"])
                if fail_first:
                    parent_id, failure = policy.calls[-1]
                    self.assertEqual(parent_id, result["revision"]["identity"]["parent_revision_id"])
                    self.assertEqual("implementation_failed", failure["status"])
                    if unused_helper:
                        self.assertEqual("changed_code_not_executed",
                                         failure["execution_gate"]["reason"])
                    else:
                        self.assertIn("RuntimeError: injected implementation failure",
                                      failure["logs"]["stderr.log"]["text"])
                    # baseline(1) + failed candidate(1) + repaired candidate(1) = 3
                    self.assertEqual(3, status["budget"]["spent"])
                self.assertEqual("sandbox_only", status["generated_code_execution"])
                self.assertTrue(status["integrity"]["ok"])
                # baseline + candidate dev observations are both present now.
                self.assertEqual(2, len(status["observations"]))
                self.assertTrue(all(r["execution_gate"]["passed"] for r in result["receipts"]))
                original_call_count = len(policy.calls)
                replay = controller.implement(hypothesis_id)
                self.assertEqual("already_observed", replay["status"])
                self.assertIsNone(replay["revision"])
                self.assertEqual(original_call_count, len(policy.calls))
                controller.store.set_hypothesis_status(
                    hypothesis_id, 1, HypothesisStatus.SUPPORTED_IN_SCOPE)
                # C1：生成代码 revision 的候选现在可以经 Worker + holdout 封读
                # 在 test split 上确认。confirm 推进 phase 至 concluded。
                confirmed = controller.confirm(sandboxed=True)
                self.assertEqual(StudyStatus.CONCLUDED.value, confirmed["phase"])
                trace = (controller.worker.root / result["receipts"][0]["job_id"]
                         / "workspace/outputs/_popper_execution.json")
                trace.write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(ProtocolError, "已变化"):
                    controller.worker.collect(result["receipts"][0]["job_id"])
                with self.assertRaisesRegex(ProtocolError, "已变化"):
                    controller.status()
            finally:
                controller.close()


class RegisteredImplementationWorkerTests(unittest.TestCase):
    """已注册实现走 worker（launch/poll/collect）而非 exp.evaluate。"""

    def test_registered_dev_executes_via_worker_not_evaluate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project, run_dir = root / "project", root / "research"
            project.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(EXAMPLE / name, project / name)
            runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](project)
            initialize(project)

            controller = None
            try:
                ResearchController.initialize(project, run_dir)
                controller = ResearchController(run_dir)

                def executor(command, workspace, env, stdout, stderr, spec):
                    subprocess.run(command, cwd=workspace, env=env, stdout=stdout,
                                   stderr=stderr, timeout=spec.timeout_seconds, check=True)

                controller.worker.executor = executor
                control_id = controller.manifest["control_hypothesis_id"]
                proposal = ActionProposal(RUN_EXPERIMENT, "baseline via worker", control_id,
                                          source="research_controller")
                with patch.object(controller.exp, "evaluate",
                                  side_effect=AssertionError(
                                      "registered path must use worker, not evaluate")):
                    observation = controller._execute_development(
                        control_id, proposal, False, True)
                self.assertEqual("dev", observation["scope"])
                # results() 只读 status='completed' 的核心 run，证明 register/complete 已落库。
                dev_runs = controller.exp.results("dev")
                self.assertTrue(dev_runs)
                self.assertTrue(all("run_id" in r and "mean" in r for r in dev_runs))
            finally:
                if controller:
                    controller.close()

    def test_trusted_local_dev_executes_via_worker_not_evaluate(self):
        """C2：trusted-local 也走 Worker（sandboxed=False），而非 exp.evaluate。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project, run_dir = root / "project", root / "research"
            project.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(EXAMPLE / name, project / name)
            runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](project)
            initialize(project)

            controller = None
            try:
                ResearchController.initialize(project, run_dir)
                controller = ResearchController(run_dir)

                captured = []
                def executor(command, workspace, env, stdout, stderr, spec):
                    captured.append(spec)
                    subprocess.run(command, cwd=workspace, env=env, stdout=stdout,
                                   stderr=stderr, timeout=spec.timeout_seconds, check=True)

                controller.worker.executor = executor
                control_id = controller.manifest["control_hypothesis_id"]
                proposal = ActionProposal(RUN_EXPERIMENT, "baseline via worker", control_id,
                                          source="research_controller")
                with patch.object(controller.exp, "evaluate",
                                  side_effect=AssertionError(
                                      "trusted-local path must use worker, not evaluate")):
                    observation = controller._execute_development(
                        control_id, proposal, True, False)
                self.assertEqual("dev", observation["scope"])
                self.assertTrue(captured, "executor should have been called via Worker")
                self.assertFalse(captured[0].sandboxed,
                                 "JobSpec must carry sandboxed=False for trusted-local")
                self.assertEqual("controller_scored_trusted_local",
                                 controller.exp.results("dev")[-1]["trust"])
            finally:
                if controller:
                    controller.close()


class WorkerProtocolTests(unittest.TestCase):
    """Worker Protocol 的传输无关产物/枚举契约：本地与远程共用同一方法语义。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.project / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.project)
        initialize(self.project)
        self.revisions = RevisionStore(root / "revisions")
        source = self.project / "model.py"
        self.manifest = self.revisions.create(
            self.project, "H1", "D1",
            [CodeEdit("model.py", source.read_text(encoding="utf-8") + "\n# revision\n",
                      file_hash(source))], "test-policy")
        self.revision_dir = self.revisions.path(self.manifest["revision_id"])

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, key="protocol-key"):
        return JobSpec(
            idempotency_key=key, revision_id=self.manifest["revision_id"],
            revision_manifest_sha256=file_hash(self.revision_dir / "revision.json"),
            design_id="D1", entrypoint="model.py", args=(),
            outputs=("outputs/result.json",), timeout_seconds=10)

    @staticmethod
    def _executor(command, workspace, env, stdout, stderr, spec):
        output = workspace / "outputs" / "result.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text('{"ok":true}', encoding="utf-8")

    def test_local_worker_lists_jobs_receipts_and_fetches_artifact(self):
        worker = LocalWorker(Path(self.temp.name) / "jobs", self.revisions,
                             self._executor)
        spec = self.spec("local-artifacts")
        worker.run(spec)
        self.assertEqual([spec.job_id], [j["job_id"] for j in worker.list_jobs()])
        receipts = worker.list_receipts()
        self.assertEqual([spec.job_id], [r["job_id"] for r in receipts])
        destination = Path(self.temp.name) / "fetched-result.json"
        worker.fetch_artifact(spec.job_id, "outputs/result.json", destination)
        self.assertEqual(receipts[0]["artifacts"]["outputs/result.json"],
                         file_hash(destination))

    def test_server_and_remote_worker_round_trip(self):
        from http.server import ThreadingHTTPServer
        from popper.research.workers.server import WorkerHandler
        worker = LocalWorker(Path(self.temp.name) / "remote-jobs", self.revisions,
                             self._executor)
        handler = type("_TestHandler", (WorkerHandler,),
                       {"worker": worker, "token": b"secret-token"})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        serving = threading.Thread(target=httpd.serve_forever, daemon=True)
        serving.start()
        try:
            with patch.dict(os.environ, {"POPPER_WORKER_TOKEN": "secret-token"}):
                remote = RemoteWorker(f"http://127.0.0.1:{port}", revisions=self.revisions)
                spec = self.spec("remote-roundtrip")
                remote.launch(spec)
                self.assertEqual("succeeded", remote.poll(spec.job_id)["status"])
                receipt = remote.collect(spec.job_id)
                self.assertEqual("succeeded", receipt["status"])
                self.assertEqual([spec.job_id], [j["job_id"] for j in remote.list_jobs()])
                self.assertEqual([spec.job_id], [r["job_id"] for r in remote.list_receipts()])
                remote.reap_stale()
                destination = Path(self.temp.name) / "remote-fetched.json"
                remote.fetch_artifact(spec.job_id, "outputs/result.json", destination)
                self.assertEqual(receipt["artifacts"]["outputs/result.json"],
                                 file_hash(destination))
        finally:
            httpd.shutdown()
            httpd.server_close()
            serving.join(timeout=5)


class _NoRootWorker:
    """协议完备、但没有任何文件系统视图的 Worker：证明控制器只依赖 Protocol。"""

    def list_jobs(self):
        return []

    def list_receipts(self):
        return []

    def reap_stale(self, job_ids=None, stale_seconds=20.0):
        return None

    def submit(self, spec):
        raise NotImplementedError

    def launch(self, spec):
        raise NotImplementedError

    def poll(self, job_id):
        raise NotImplementedError

    def collect(self, job_id):
        raise NotImplementedError

    def cancel(self, job_id):
        raise NotImplementedError

    def failure_context(self, job_id, max_bytes=8192):
        raise NotImplementedError

    def fetch_artifact(self, job_id, name, destination):
        raise NotImplementedError


class ControllerProtocolOnlyTests(unittest.TestCase):
    def test_controller_has_no_worker_filesystem_dependency(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project, run_dir = root / "project", root / "research"
            project.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(EXAMPLE / name, project / name)
            runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](project)
            initialize(project)
            ResearchController.initialize(project, run_dir)
            controller = ResearchController(run_dir, worker=_NoRootWorker())
            try:
                status = controller.status()
                self.assertIn("phase", status)
                self.assertEqual([], status["execution_gates"])
            finally:
                controller.close()


class StagedInputArtifactsTests(unittest.TestCase):
    """输入制品的落位名必须与调用契约声明同源：args 指向的文件就是被落位的文件。"""

    def setUp(self):
        from popper import domains
        from popper.core import evaluator_pack
        from popper.domains.protocol import Invocation

        class WorkloadProbePack(type(evaluator_pack("mse-v1"))):
            pack_id, evaluator_id = "workload-probe", "workload-probe-v1"
            _metrics = (domains.MetricSpec(name="probe_error", direction="min",
                                           unit="absolute_error", value_domain=(0, None)),)
            _entry = {"id": "workload-probe-v1",
                      "metric": {"name": "probe_error", "direction": "min"},
                      "definition": "输入落位名探针",
                      "dataset": "rows[id,x,y] where x and y are finite numbers"}

            def invocation(self):
                return Invocation(
                    args=(("--workload", "inputs"), ("--output", "prediction"),
                          ("--config", "config"), ("--seed", "seed")),
                    inputs=(("inputs", "workload.json"), ("config", "config.json")),
                    prediction="measurements-{seed}.json")

        pack = domains.register(WorkloadProbePack())
        self.addCleanup(domains.protocol._PACKS.pop, "workload-probe-v1", None)
        self.invocation = pack.invocation()

    def sources(self, **overrides):
        values = {"inputs": ("workload-source.json", "a" * 64),
                  "config": ("config-source.json", "b" * 64)}
        values.update(overrides)
        return values

    def test_declared_basenames_drive_both_args_and_staged_targets(self):
        from popper.core import evaluator_pack
        args, outputs = job_invocation(evaluator_pack("workload-probe-v1"), 7)
        self.assertIn("inputs/workload.json", args)
        self.assertIn("inputs/config.json", args)
        self.assertEqual(("outputs/measurements-7.json",), outputs)
        artifacts = staged_input_artifacts(self.invocation, self.sources())
        # 核心不变量：args 里的每个输入路径都对应一个已落位的制品（顺序 = 声明顺序）。
        self.assertEqual(["workload.json", "config.json"],
                         [item.target for item in artifacts])
        for item in artifacts:
            self.assertIn("inputs/" + item.target, args)
        self.assertEqual(["development_features_without_labels", "frozen_intervention"],
                         [item.role for item in artifacts])
        self.assertEqual("workload-source.json", artifacts[0].source)

    def test_declared_role_without_source_fails_loudly(self):
        with self.assertRaisesRegex(ProtocolError, "config"):
            staged_input_artifacts(self.invocation, {"inputs": ("w.json", "a" * 64)})

    def test_source_for_undeclared_role_is_not_staged(self):
        artifacts = staged_input_artifacts(
            self.invocation, self.sources(train=("train.json", "c" * 64)))
        self.assertNotIn("train.json", [item.target for item in artifacts])


if __name__ == "__main__":
    unittest.main()
