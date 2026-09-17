"""可恢复的证据驱动 Research Controller（R1/R3 首个真实执行切片）。"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .. import sandbox
from ..core import (Experiment, ProtocolError, _require_holdout_seal, canonical,
                    dataset, digest, evaluator_pack, file_hash, inside, model_inputs,
                    read_json, score, write_json)
from ..harness import for_policy as harness_for_policy
from .actions import (ADD_CONTROL, REQUEST_CONFIRMATION, RUN_EXPERIMENT, STOP,
                      CONCLUDE_SCOPE_BOUNDARY, IMPLEMENT_REVISION,
                      REQUEST_SCOPE_BOUNDARY_CONFIRMATION,
                      REPAIR_IMPLEMENTATION, ActionProposal)
from .context import build_context
from .contracts import (Decision, DesignStatus, ExperimentDesign, Hypothesis,
                        HypothesisStatus, Observation, RunStatus, Study,
                        StudyStatus)
from .evaluation_service import IndependentEvaluator, scoring_code_hash
from .models import EvidenceDrivenPolicy
from .revisions import RevisionStore
from .store import ResearchStore
from .workers import (JobSpec, LocalWorker, TERMINAL_JOB_STATUSES, Worker,
                      job_invocation, staged_input_artifacts)


class RevisionExecutionError(ProtocolError):
    """A completed worker failure, eligible for bounded implementation repair."""

    def __init__(self, revision_id, status, error_type):
        self.revision_id = revision_id
        self.status = status
        super().__init__(f"CodeRevision 执行失败: {status}/{error_type}")


class ResearchController:
    """在已初始化 Popper 实验上执行自主、可审计的研究决策循环。"""

    MANIFEST = "research.json"

    @classmethod
    def initialize(cls, project, run_dir, policy=None, budget_cap=None,
                   confirmation_contract=None, confirmation_public_key=None):
        project, run_dir = Path(project).resolve(), Path(run_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = run_dir / cls.MANIFEST
        if manifest_path.exists():
            controller = cls(run_dir, policy=policy)
            try:
                return controller.status()
            finally:
                controller.close()
        exp = Experiment(project)
        try:
            state = exp.verify_inputs()
            spec = state["spec"]
        finally:
            exp.close()
        if state["phase"] != "searching":
            raise ProtocolError("只能从尚未冻结、未消费确认集的实验初始化 research")
        external_confirmation = None
        if (confirmation_contract is None) != (confirmation_public_key is None):
            raise ProtocolError("确认合同与独立固定的服务公钥必须同时提供")
        if confirmation_contract is not None:
            from .confirmation_contracts import validate_contract
            contract = validate_contract(confirmation_contract, confirmation_public_key)
            expected = {"train_sha256": state["input_hashes"][spec["train"]],
                        "dev_sha256": state["input_hashes"][spec["dev"]],
                        "evaluator_id": state["evaluator_id"], "evaluator_hash": state["evaluator_hash"],
                        "scoring_code_sha256": scoring_code_hash(), "seeds": spec["seeds"],
                        "min_effect": spec["min_improvement"]}
            for name, value in expected.items():
                if contract[name] != value:
                    raise ProtocolError(f"确认合同与预注册开发实验不一致: {name}")
            slices = spec.get("analysis_slices", [])
            if contract.get("analysis_slices", []) != slices or (
                    slices and contract["schema_version"] != "2.0"):
                raise ProtocolError("确认合同未冻结预注册分析切片")
            external_confirmation = {"contract": confirmation_contract,
                                     "pinned_public_key": confirmation_public_key}
        policy = policy or EvidenceDrivenPolicy()
        generated = (policy.generate_hypotheses(spec["objective"], spec["candidates"])
                     if hasattr(policy, "generate_hypotheses") else None)
        if generated is None:
            generated = [_default_hypothesis(config, spec["metric"])
                         for config in spec["candidates"]]
        if len(generated) != len(spec["candidates"]):
            raise ProtocolError("假设数量必须与预注册候选数量相同")

        input_identity = {"input_hashes": state["input_hashes"],
                          "evaluator_hash": state["evaluator_hash"]}
        family_id = "F-" + digest(input_identity)[:16]
        study_id = "S-" + digest({"family_id": family_id, "run_dir": str(run_dir)})[:16]
        control_hypothesis_id = "H-control-" + digest(spec["baseline"])[:12]
        candidate_ids = ["H-" + digest({"index": i, "config": config})[:16]
                         for i, config in enumerate(spec["candidates"])]
        configs = {control_hypothesis_id: spec["baseline"]}
        configs.update(zip(candidate_ids, spec["candidates"]))
        cap = float(budget_cap if budget_cap is not None else spec["budget"] + 3)
        if cap < 4:
            raise ProtocolError("research budget 至少需要覆盖基线、一个候选和两次确认计分")
        manifest = {
            "schema_version": "1.0", "project": str(project), "study_id": study_id,
            "family_id": family_id, "control_hypothesis_id": control_hypothesis_id,
            "candidate_hypothesis_ids": candidate_ids, "configs": configs,
            "metric": spec["metric"], "scorer_id": state["evaluator_id"],
            "scorer_hash": state["evaluator_hash"], "seeds": spec["seeds"],
            "scoring_code_sha256": scoring_code_hash(),
            "min_meaningful_effect": spec["min_improvement"],
            "analysis_slices": spec.get("analysis_slices", []),
            "budget_cap": cap, "input_identity": input_identity,
            "capability_mode": ("model_generated_hypotheses" if generated and
                                hasattr(policy, "generate_hypotheses")
                                else "registered_hypothesis_baseline"),
            "policy": getattr(policy, "name", type(policy).__name__),
            "hypotheses": generated,
        }
        if external_confirmation is not None:
            manifest["external_confirmation"] = external_confirmation
        write_json(manifest_path, manifest)
        store = ResearchStore(run_dir / "research.sqlite")
        try:
            scope = canonical({"project": str(project),
                               "manifest_sha256": file_hash(manifest_path),
                               "trust": "development_evaluation_in_separate_process_same_account"})
            store.create_study(Study(study_id, family_id, spec["objective"], scope,
                                     data_exposure=(spec["train"], spec["dev"]),
                                     budget_cap=cap))
            _create_hypothesis_and_design(
                store, manifest, control_hypothesis_id,
                {"mechanism": "预注册基线作为方向统一效应的对照",
                 "applicability": "当前固定开发划分",
                 "predictions": ["提供候选比较的控制值"],
                 "falsification": ["基线无法成功执行或独立重算"],
                 "alternatives": []}, is_control=True)
            for hypothesis_id, hypothesis in zip(candidate_ids, generated):
                _create_hypothesis_and_design(store, manifest, hypothesis_id, hypothesis)
        finally:
            store.close()
        controller = cls(run_dir, policy=policy)
        try:
            return controller.status()
        finally:
            controller.close()

    def __init__(self, run_dir, policy=None, worker=None, harness=None):
        self.run_dir = Path(run_dir).resolve()
        self.manifest_path = self.run_dir / self.MANIFEST
        if not self.manifest_path.is_file():
            raise ProtocolError("research 尚未初始化")
        self.manifest = read_json(self.manifest_path)
        self.project = Path(self.manifest["project"]).resolve()
        self.exp = Experiment(self.project)
        self.store = ResearchStore(self.run_dir / "research.sqlite")
        try:
            self.policy = policy or EvidenceDrivenPolicy()
            # 编码 Agent 只提案；写入权、预算与覆盖门禁仍在内核（RevisionStore/本类）手里。
            # 自带 propose_revision 的策略被降级成 harness 的一种，控制器不再直连提案者。
            self.harness = harness or harness_for_policy(self.policy)
            self.evaluator = IndependentEvaluator(self.project, self.run_dir / "evaluations")
            self.revisions = RevisionStore(self.run_dir / "revisions")
            # 可通过构造注入替换后端（默认本地分离式 worker），面向 Worker Protocol 解耦。
            self.worker: Worker = worker if worker is not None else LocalWorker(
                self.run_dir / "jobs", self.revisions)
            self._verify_inputs()
            # 启动对账：回收上次崩溃/中断遗留的开发集悬空预留，不依赖崩溃前补偿是否跑完。
            self.store.reconcile_budget(self.manifest["study_id"])
        except Exception:
            # 构造失败也必须归还 sqlite 句柄：未关闭的连接会锁住研究库，
            # 让上层无法删除/重建研究目录，也让失败原因被后续错误掩盖。
            self.close()
            raise

    def close(self):
        if getattr(self, "store", None):
            self.store.close()
            self.store = None
        if getattr(self, "exp", None):
            self.exp.close()
            self.exp = None

    def _verify_inputs(self):
        integrity = self.store.verify()
        if not integrity["ok"]:
            raise ProtocolError(f"research store 完整性失败: {integrity['reason']}")
        study = self.store.get("study", self.manifest["study_id"])
        scope = json.loads(study["scope"])
        if file_hash(self.manifest_path) != scope["manifest_sha256"]:
            raise ProtocolError("research manifest 已修改；不能静默继续")
        bound_scoring_code = self.manifest.get("scoring_code_sha256")
        if bound_scoring_code is not None and bound_scoring_code != scoring_code_hash():
            raise ProtocolError("独立计分实现与 study 注册版本不同；旧研究不能静默继续")
        state = self.exp.verify_inputs()
        current = {"input_hashes": state["input_hashes"],
                   "evaluator_hash": state["evaluator_hash"]}
        if current != self.manifest["input_identity"]:
            raise ProtocolError("底层实验身份与 research manifest 不一致")
        for job in self.worker.list_jobs():
            if job["status"] in TERMINAL_JOB_STATUSES:
                self.worker.collect(job["job_id"])
        # 重连：把「停在 running 但 supervisor 已死」的 job 判基础设施失败（不重跑）。
        self.worker.reap_stale()
        external = self._external_submission_record()
        if external is not None:
            from .confirmation_bundle import verify_bundle
            enrollment = self.manifest["external_confirmation"]
            bundle, _ = verify_bundle(self._external_bundle_dir(), enrollment["pinned_public_key"])
            if digest(bundle) != external["submission_sha256"]:
                raise ProtocolError("已冻结的独立确认提交发生变化")
            receipt_path = self.run_dir / "external-confirmation" / "result.json"
            result_decision = self.store.get("decision", "DEC-result-" + bundle["submission_id"])
            if receipt_path.exists():
                from .confirmation_contracts import validate_result
                envelope = read_json(receipt_path)
                validate_result(envelope, enrollment["contract"], bundle, enrollment["pinned_public_key"])
                if result_decision is not None and json.loads(result_decision["rationale"])["external_receipt_sha256"] != digest(envelope):
                    raise ProtocolError("独立确认回执与已记录结论不同")
            elif result_decision is not None:
                raise ProtocolError("已记录的独立确认回执缺失")
        return state

    def _external_bundle_dir(self):
        return self.run_dir / "external-confirmation" / "bundle"

    def _external_submission_record(self):
        records = [item for item in self.store.list("decision")
                   if item["study_id"] == self.manifest["study_id"]
                   and item["action"] == "freeze_external_confirmation"]
        if len(records) > 1:
            raise ProtocolError("同一研究不能冻结多份独立确认提交")
        return json.loads(records[0]["rationale"]) if records else None

    def _external_is_frozen(self):
        return self._external_submission_record() is not None or self._external_bundle_dir().exists()

    def _require_scoring_binding(self):
        if self.manifest.get("scoring_code_sha256") is None:
            raise ProtocolError(
                "旧 research 未绑定计分源码，只允许读取和审计；请创建新的 research 运行目录。"
                "新目录仍使用原研究家族预算，不能把历史分数与新计分实现混用")

    def _record(self, proposal, observation_refs=(), budget_request=0.0):
        study = self.store.get("study", self.manifest["study_id"])
        ordinal = len(self.store.list("decision")) + 1
        decision_id = "DEC-" + digest({"ordinal": ordinal,
                                        "proposal": proposal.as_dict(),
                                        "refs": list(observation_refs)})[:18]
        return self.store.add_decision(Decision(
            decision_id, self.manifest["study_id"], study["state_version"],
            tuple(observation_refs), proposal.kind, proposal.alternatives,
            proposal.rationale, budget_request,
            actor=proposal.source, model=proposal.model))

    def _observation_for_design(self, design_id, scope="dev"):
        return next((o for o in self.store.list("observation")
                     if o.get("design_id") == design_id and o.get("scope") == scope), None)

    def _record_development_observations(self, run_id, evaluated):
        """Persist the global score and every preregistered diagnostic slice."""
        common = {"run_id": run_id, "scorer_id": self.manifest["scorer_id"],
                  "unit": self.manifest["metric"]["name"],
                  "artifact_id": evaluated["artifact_id"],
                  "artifact_sha256": evaluated["artifact_sha256"],
                  "evaluator_service_hash": evaluated["service_sha256"],
                  "trust": evaluated["trust"]}
        observation_id = "OBS-" + digest(
            {"run_id": run_id, "request": evaluated["request_sha256"], "scope": "dev"})[:18]
        self.store.observations.add(Observation(
            observation_id, value=evaluated["mean"], uncertainty=evaluated["std"],
            scope="dev", selector="mean", **common))
        for index, item in enumerate(evaluated.get("slices", [])):
            scope = f"dev:slice:{item['slice_id']}"
            slice_id = "OBS-" + digest(
                {"run_id": run_id, "request": evaluated["request_sha256"], "scope": scope})[:18]
            self.store.observations.add(Observation(
                slice_id, value=item["mean"], uncertainty=item["std"], scope=scope,
                selector=f"slices.{index}.mean", **common))
        return self.store.get("observation", observation_id)

    def _current_design_id(self, hypothesis_id):
        hypothesis = self.store.get("hypothesis", hypothesis_id)
        return _design_id(hypothesis_id, hypothesis["version"])

    def _registered_revision(self, state, design_id):
        """把项目已注册代码镜像为不可变、内容寻址的 CodeRevision（无编辑）。

        已注册实现不经过 propose_revision，拿不到编辑，无法用 RevisionStore.create；
        这里复刻 confirmation_runner 的「冻结注册候选」做法，直接落一份 immutable
        快照，让已注册实现与生成 revision 共用同一条分离式 worker 执行路径。
        """
        spec = state["spec"]
        identity = {
            "kind": "registered_implementation_dev",
            "design_id": design_id,
            "source_input_hashes": state["input_hashes"],
            "entrypoint": spec["entrypoint"],
            "files": {name: state["input_hashes"][name] for name in spec["code_files"]},
        }
        revision_id = "REV-" + digest(identity)[:24]
        target = (self.revisions.root / revision_id).resolve()
        if target.exists():
            manifest = self.revisions.verify(revision_id)
            if manifest["identity"] != identity:
                raise ProtocolError("registered revision identity 冲突")
            return manifest
        staging = (self.revisions.root / (revision_id + ".staging")).resolve()
        try:
            code_dir = staging / "code"
            for name in spec["code_files"]:
                source = (self.project / name).resolve()
                if not source.is_file() or file_hash(source) != state["input_hashes"][name]:
                    raise ProtocolError(f"已注册代码发生修改: {name}")
                destination = code_dir / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            files = {str(path.relative_to(code_dir)).replace("\\", "/"): file_hash(path)
                     for path in sorted(code_dir.rglob("*.py"))}
            manifest = {"schema_version": "1.0", "revision_id": revision_id,
                        "identity": identity, "entrypoint": spec["entrypoint"],
                        "registered_code_files": sorted(spec["code_files"]),
                        "added_code_files": [], "files": files,
                        "status": "immutable"}
            staging.mkdir(parents=True, exist_ok=True)
            write_json(staging / "revision.json", manifest)
            staging.rename(target)
            return manifest
        except Exception:
            if staging.exists() and staging.is_relative_to(self.revisions.root):
                shutil.rmtree(staging)
            raise

    def _staged_inputs(self, invocation, inputs_dir, state, rows, config, evaluator_id):
        """按声明的输入角色落位输入制品，返回 InputArtifact tuple。

        落位名与 job_invocation() 生成的 args 同源（都来自 invocation.inputs），
        否则域包换个输入基名就会静默产生「候选读不到输入」的失败。
        """
        sources = {}
        for role, basename in invocation.inputs:
            target = inputs_dir / basename
            if role == "train":
                train = state["spec"]["train"]
                sources[role] = (self.project / train, state["input_hashes"][train])
            elif role == "inputs":
                write_json(target, model_inputs(rows, evaluator_id))
                sources[role] = (target, file_hash(target))
            elif role == "config":
                write_json(target, config)
                sources[role] = (target, file_hash(target))
            else:
                raise ProtocolError(f"未支持的调用输入角色: {role!r}")
        return staged_input_artifacts(invocation, sources)

    def _evaluate_registered_via_worker(self, config, design_id, sandboxed=True):
        """通过分离式 worker 执行已注册代码（dev split）。

        与 ``self.exp.evaluate()`` 的同步路径产出相同的核心 result 契约，只是把 seed 执行
        从控制器调用栈迁到 worker（detach + 可重连），并复用 ``register_run`` /
        ``complete_run`` 分离出的 DB 生命周期，避免打破 freeze/confirm/replay。

        ``sandboxed`` 透传到 JobSpec：True 走沙箱后端（生成代码的安全保证），False 走
        trusted-local 进程组（无后端平台可用，用户自负代码可信）。

        已注册实现（控制基线 / 未生成 revision 的候选）走本方法。**生成代码 revision
        的候选在 test split 上确认** 走 ``_evaluate_via_worker(..., split="test", revision=...)``，
        以便对齐 holdout 封读窗口并复用 dev 阶段已落盘的不可变 revision。
        """
        state = self.exp.verify_inputs()
        revision = self._registered_revision(state, design_id)
        return self._evaluate_via_worker(config, design_id, revision, "dev", sandboxed=sandboxed)

    def _evaluate_via_worker(self, config, design_id, revision, split="dev", sandboxed=True):
        """通过分离式 worker 执行给定 CodeRevision，并把核心 run 注册/完成到 Experiment。

        与 ``self.exp.evaluate()`` 同步路径产出相同的核心 result 契约，只是把执行迁到
        worker（detach + 可重连）。``split`` 决定数据划分；``split="test"`` 启用
        holdout sealing（保留集读隔离窗口覆盖整段 Worker 执行）。

        ``revision`` 是已 ``verify`` 的 CodeRevision manifest。已注册实现需先经
        ``_registered_revision`` 落一份不可变快照再传入；生成代码 revision 直接复用
        dev 阶段已落盘的 revision（revision_id 与 dev run 一致）。

        ``sandboxed`` 透传到 JobSpec：True 走沙箱后端（生成代码的安全保证），False 走
        trusted-local 进程组（无后端平台可用，用户自负代码可信）。
        """
        if split == "test":
            # 必须在 register_run（提交 test_consumed）之前确认本平台能封住保留集，
            # 否则用户会在没有任何隔离的情况下白丢一次测试访问。与 evaluate() 内
            # 同样的检查互补：test split 必然经过 begin_confirmation → _evaluate_via_worker，
            # 所以这里先过门，begin_confirmation 里的同一检查是兜底。
            _require_holdout_seal(sandboxed, "test")
        rid, state, prior = self.exp.register_run(config, split)
        if prior is not None:
            return prior
        started = time.monotonic()
        try:
            spec = state["spec"]
            evaluator_id = state["evaluator_id"]
            revision_id = revision["revision_id"]
            revision_sha = file_hash(self.revisions.path(revision_id) / "revision.json")
            run_dir = self.exp.home / "runs" / rid
            run_dir.mkdir(parents=True, exist_ok=True)
            rows = dataset(self.project / spec[split], evaluator_id)
            inputs_dir = self.run_dir / "revision-inputs" / revision_id
            inputs_dir.mkdir(parents=True, exist_ok=True)
            pack = evaluator_pack(evaluator_id)
            artifacts = self._staged_inputs(
                pack.invocation(), inputs_dir, state, rows, config, evaluator_id)
            seed_specs = []
            for seed in spec["seeds"]:
                args, outputs = job_invocation(pack, seed)
                output = outputs[0]
                job = JobSpec(
                    idempotency_key=f"{rid}:seed:{seed}:{split}-v1",
                    revision_id=revision_id, revision_manifest_sha256=revision_sha,
                    design_id=design_id, entrypoint=spec["entrypoint"],
                    args=args, inputs=artifacts, outputs=outputs,
                    timeout_seconds=spec["timeout_seconds"],
                    mem_limit_mb=spec.get("mem_limit_mb"), sandboxed=sandboxed)
                seed_specs.append((seed, job, output))
            # 保留集读隔离窗口必须覆盖整段 Worker 执行（submit→poll→collect→score）：
            # 候选进程在任何时刻都读不到测试标签文件。控制器自身在窗口内不需要
            # 再读该文件——rows 已在窗口外读入内存，scoring 也用内存 rows。
            sealed = [inside(self.project, spec["test"])] if sandboxed and split == "test" else []
            with sandbox.sealed_reads(sealed):
                job_ids = [job.job_id for _, job, _ in seed_specs]
                for _, job, _ in seed_specs:
                    self.worker.launch(job)
                deadline = time.monotonic() + spec["timeout_seconds"] + 120.0
                while True:
                    statuses = {job_id: self.worker.poll(job_id) for job_id in job_ids}
                    if all(status["status"] in TERMINAL_JOB_STATUSES
                           for status in statuses.values()):
                        break
                    if time.monotonic() >= deadline:
                        self.worker.reap_stale(job_ids)
                        statuses = {job_id: self.worker.poll(job_id) for job_id in job_ids}
                        if all(status["status"] in TERMINAL_JOB_STATUSES
                               for status in statuses.values()):
                            break
                        raise ProtocolError("已注册实现 job 未在期限内进入终态")
                    time.sleep(0.5)
                points = []
                for seed, job, output in seed_specs:
                    receipt = self.worker.collect(job.job_id)
                    if receipt["status"] != "succeeded":
                        raise ProtocolError(
                            f"已注册实现执行失败: {receipt['status']}/{receipt.get('error_type')}")
                    prediction_path = run_dir / pack.invocation().prediction_name(seed)
                    self.worker.fetch_artifact(job.job_id, output, prediction_path)
                    points.append({"seed": seed,
                                   "value": score(rows, read_json(prediction_path), evaluator_id)})
            result = self.exp.build_result(rid, state, config, split, points, started, sandboxed)
            self.exp.complete_run(rid, result)
            return result
        except Exception as error:
            self.exp.fail_run(rid, type(error).__name__)
            raise ProtocolError(
                f"执行失败 {rid}: {type(error).__name__}: {error}") from error

    def _execute_development(self, hypothesis_id, proposal, trusted_local, sandboxed):
        design_id = self._current_design_id(hypothesis_id)
        existing = self._observation_for_design(design_id)
        if existing:
            return existing
        prior_runs = [r for r in self.store.list("run") if r["design_id"] == design_id]
        recoverable = next((r for r in reversed(prior_runs)
                            if r["status"] in {RunStatus.QUEUED.value,
                                               RunStatus.RUNNING.value,
                                               RunStatus.SUCCEEDED.value}), None)
        attempt = len(prior_runs) + (0 if recoverable else 1)
        run_id = (recoverable["run_id"] if recoverable
                  else f"RUN-{design_id}-{attempt}")
        ledger = self.store.budget(self.manifest["study_id"])
        try:
            if recoverable is None:
                # 记账 + Run 登记 + 状态推进同库同事务：登记失败时预留一并回滚，
                # 不会留下悬空预留，因此不再需要手写 settle(0.0) 补偿。
                with self.store.transaction():
                    reservation = ledger.reserve(self.manifest["family_id"], run_id, 1.0)
                    self._record(proposal, budget_request=1.0)
                    self.store.runs.register(run_id, design_id)
                    self.store.runs.set_status(run_id, RunStatus.RUNNING)
            else:
                reservation = ledger.reserve(self.manifest["family_id"], run_id, 1.0)
                if recoverable["status"] == RunStatus.QUEUED.value:
                    self.store.runs.set_status(run_id, RunStatus.RUNNING)
            try:
                state = self._verify_inputs()
                receipt_path = self.run_dir / "receipts" / f"{run_id}.json"
                if recoverable and recoverable["status"] == RunStatus.SUCCEEDED.value:
                    if not receipt_path.is_file():
                        raise ProtocolError("成功 Run 缺少评估回执，不能猜测 Observation")
                    receipt = read_json(receipt_path)
                    result = receipt["core_result"]
                    registered = next((r for r in self.exp.results("dev")
                                       if r["run_id"] == result.get("run_id")), None)
                    if registered != result:
                        raise ProtocolError("恢复回执与底层权威 Run 不一致")
                    evaluated = self.evaluator.score_core_result(state, result)
                else:
                    result = self._evaluate_registered_via_worker(
                        self.manifest["configs"][hypothesis_id], design_id, sandboxed=sandboxed)
                    evaluated = self.evaluator.score_core_result(state, result)
                if abs(float(evaluated["mean"]) - float(result["mean"])) > 1e-12:
                    self.store.set_study_status(self.manifest["study_id"],
                                                StudyStatus.INTEGRITY_FAILED)
                    self.store.runs.set_status(run_id, RunStatus.CANCELLED)
                    ledger.settle(reservation["reservation_id"], 1.0)
                    raise ProtocolError("底层结果与独立评估结果不一致")
                (self.run_dir / "receipts").mkdir(parents=True, exist_ok=True)
                write_json(self.run_dir / "receipts" / f"{run_id}.json",
                           {"core_result": result, "evaluation": evaluated})
                if self.store.get("run", run_id)["status"] == RunStatus.RUNNING.value:
                    self.store.runs.set_status(run_id, RunStatus.SUCCEEDED)
                observation = self._record_development_observations(run_id, evaluated)
                if reservation["status"] == "reserved":
                    ledger.settle(reservation["reservation_id"], 1.0)
                return observation
            except Exception:
                current = self.store.get("run", run_id)
                if current and current["status"] == RunStatus.RUNNING.value:
                    self.store.runs.set_status(run_id, RunStatus.IMPLEMENTATION_FAILED)
                if reservation["status"] == "reserved":
                    ledger.settle(reservation["reservation_id"], 1.0)
                raise
        finally:
            ledger.close()

    def implement(self, hypothesis_id, parent_revision_id=None, gpu_count=0):
        """Generate, freeze, execute and independently score one code revision.

        Generated code always goes through LocalWorker's OS sandbox. There is no
        trusted-local switch on this path.
        """
        self._verify_inputs()
        self._require_scoring_binding()
        if self._external_is_frozen():
            raise ProtocolError("独立确认提交已冻结，不能继续生成或修复代码")
        if self.harness is None:
            raise ProtocolError(
                "当前策略不能生成代码；请配置支持 propose_revision 的模型策略，"
                "或改用 --harness 指定编码 Agent 接入")
        hypothesis = self.store.get("hypothesis", hypothesis_id)
        if hypothesis is None or hypothesis_id == self.manifest["control_hypothesis_id"]:
            raise ProtocolError("只能为当前 study 的候选 hypothesis 实现代码")
        if hypothesis["study_id"] != self.manifest["study_id"]:
            raise ProtocolError("hypothesis 不属于当前 study")
        design_id = self._current_design_id(hypothesis_id)
        existing = self._observation_for_design(design_id)
        if existing:
            # Do not generate a fresh, unexecuted revision and attach old evidence.
            return {"revision": None, "observation": existing,
                    "status": "already_observed"}
        parent = self.revisions.verify(parent_revision_id) if parent_revision_id else None
        if parent:
            if (parent["identity"]["hypothesis_id"] != hypothesis_id
                    or parent["identity"]["design_id"] != design_id):
                raise ProtocolError("父 revision 必须属于同一 hypothesis/design")
            code_root = self.revisions.path(parent_revision_id) / "code"
            source_hashes = parent["files"]
        else:
            state = self._verify_inputs()
            code_root = self.project
            source_hashes = {name: state["input_hashes"][name]
                             for name in state["spec"]["code_files"]}
        code_files = [{"path": name, "sha256": sha,
                       "content": (code_root / name).read_text(encoding="utf-8")}
                      for name, sha in sorted(source_hashes.items())]
        previous_failure = None
        if parent_revision_id:
            previous_jobs = self.worker.list_receipts()
            failed = [r for r in previous_jobs
                      if r.get("revision_id") == parent_revision_id
                      and r.get("status") != "succeeded"]
            if failed:
                previous_failure = self.worker.failure_context(failed[-1]["job_id"])
        proposed = self.harness.propose_revision(
            self.store.get("study", self.manifest["study_id"])["question"], hypothesis,
            self.manifest["configs"][hypothesis_id], code_files,
            parent_revision=parent_revision_id, failure=previous_failure)
        proposal_source = self.harness.name
        proposal_model = getattr(self.harness, "model", None)
        if not proposed["edits"]:
            if parent_revision_id is not None:
                raise ProtocolError("implementation repair cannot discard its parent revision")
            proposal = ActionProposal(
                RUN_EXPERIMENT,
                f"{proposed['rationale']} implementation_mode=registered_implementation",
                hypothesis_id, alternatives=("generate_code_revision",),
                source=proposal_source, model=proposal_model)
            observation = self._execute_development(
                hypothesis_id, proposal, trusted_local=False, sandboxed=True)
            return {"status": "registered_implementation", "revision": None,
                    "receipts": [], "observation": observation}
        revision = self.revisions.create(
            self.project, hypothesis_id, design_id, proposed["edits"],
            actor=proposal_source, parent_revision_id=parent_revision_id)
        revision_id = revision["revision_id"]
        action = REPAIR_IMPLEMENTATION if parent_revision_id else IMPLEMENT_REVISION
        proposal = ActionProposal(
            action, f"{proposed['rationale']} CodeRevision={revision_id}", hypothesis_id,
            alternatives=("保留原始实现",), source=proposal_source, model=proposal_model)
        run_id = "RUN-" + digest({"revision_id": revision_id, "split": "dev"})[:24]
        ledger = self.store.budget(self.manifest["study_id"])
        # C1: core.runs 的 rid（uuid）独立于 controller 的 run_id（revision 摘要）。
        # core.runs 用于 freeze/confirm/replay 选 best candidate + 落核心 result；
        # controller run_id 用于 research Run / budget / observations。两者通过
        # config_hash 关联（register_run 用 config_hash，freeze 用 config 选 best）。
        # None 表示尚未 register（异常路径可能未到达），fail_run/complete_run 跳过。
        core_rid = None
        core_prior = None
        core_state = None
        core_started = 0.0
        try:
            existing_run = self.store.get("run", run_id)
            if existing_run and existing_run["status"] in {
                    RunStatus.INFRA_FAILED.value, RunStatus.IMPLEMENTATION_FAILED.value,
                    RunStatus.CANCELLED.value}:
                raise ProtocolError(
                    "该 CodeRevision 已终态失败；修复必须创建带 parent_revision 的新版本")
            # 记账 + Run 登记 + 状态推进同库同事务：登记失败时预留一并回滚。
            with self.store.transaction():
                reservation = ledger.reserve(self.manifest["family_id"], run_id, 1.0)
                if existing_run is None:
                    self._record(proposal, budget_request=1.0)
                    self.store.runs.register(run_id, design_id)
                    self.store.runs.set_status(run_id, RunStatus.RUNNING)
                elif existing_run["status"] == RunStatus.QUEUED.value:
                    self.store.runs.set_status(run_id, RunStatus.RUNNING)
            state = self._verify_inputs()
            # C1: 把 candidate 的 dev run 也注册到 core.runs（让 freeze 能选出该候选）。
            # registered-implementation 路径已在 _evaluate_registered_via_worker 内做
            # 同样的事；生成代码 revision 路径此前只在 store.runs 登记，core.runs 缺
            # 该候选 → freeze 报「需要成功的基线和至少一个候选」。register_run 是原子的：
            # 失败回滚，不留 running；prior 非 None 表示同 config_hash+split 已完成过
            # （理论上 controller 的 existing_run 检查会先拦下重复执行），此时不重写
            # core.runs，Worker 仍跑以收集 receipts/execution_gate，但不再 complete_run。
            config = self.manifest["configs"][hypothesis_id]
            core_rid, core_state, core_prior = self.exp.register_run(config, "dev")
            core_started = time.monotonic()
            inputs_dir = self.run_dir / "revision-inputs" / revision_id
            inputs_dir.mkdir(parents=True, exist_ok=True)
            dev_rows = dataset(self.project / state["spec"]["dev"], state["evaluator_id"])
            pack = evaluator_pack(state["evaluator_id"])
            artifacts = self._staged_inputs(
                pack.invocation(), inputs_dir, state, dev_rows,
                config, state["evaluator_id"])
            predictions = []
            receipts = []
            revision_sha = file_hash(self.revisions.path(revision_id) / "revision.json")
            seed_specs = []
            seed_outputs = []
            for seed in self.manifest["seeds"]:
                args, outputs = job_invocation(pack, seed)
                output = outputs[0]
                spec = JobSpec(
                    idempotency_key=f"{run_id}:seed:{seed}:execution-v1", revision_id=revision_id,
                    revision_manifest_sha256=revision_sha, design_id=design_id,
                    entrypoint=revision["entrypoint"],
                    args=args, inputs=artifacts, outputs=outputs,
                    timeout_seconds=state["spec"]["timeout_seconds"],
                    mem_limit_mb=state["spec"].get("mem_limit_mb"), gpu_count=gpu_count,
                    require_edit_coverage=True)
                seed_specs.append(spec)
                seed_outputs.append((seed, output))
            # 异步 submit→poll→collect：控制器不阻塞于单个 seed 的执行，每个 spec 由
            # 分离 supervisor（或注入 executor 的进程内路径）推进。GPU job 串行以免
            # 多个 seed 争抢同一批设备；CPU job 保持并发。
            job_ids = [spec.job_id for spec in seed_specs]
            max_in_flight = 1 if gpu_count else len(seed_specs)
            batches = (len(seed_specs) + max_in_flight - 1) // max_in_flight
            deadline = time.monotonic() + max(
                spec.timeout_seconds for spec in seed_specs) * batches + 120.0
            next_to_launch = 0
            in_flight = 0
            while next_to_launch < len(seed_specs) or in_flight > 0:
                while next_to_launch < len(seed_specs) and in_flight < max_in_flight:
                    self.worker.launch(seed_specs[next_to_launch])
                    next_to_launch += 1
                    in_flight += 1
                statuses = {}
                while True:
                    statuses = {jid: self.worker.poll(jid)
                                for jid in job_ids[:next_to_launch]}
                    if any(s["status"] in TERMINAL_JOB_STATUSES
                           for s in statuses.values()):
                        break
                    if time.monotonic() >= deadline:
                        self.worker.reap_stale(job_ids[:next_to_launch])
                        statuses = {jid: self.worker.poll(jid)
                                    for jid in job_ids[:next_to_launch]}
                        if any(s["status"] in TERMINAL_JOB_STATUSES
                               for s in statuses.values()):
                            break
                        raise ProtocolError("本地 worker job 未在期限内进入终态")
                    time.sleep(0.5)
                in_flight = sum(1 for s in statuses.values()
                                if s["status"] not in TERMINAL_JOB_STATUSES)
            for spec, (seed, output) in zip(seed_specs, seed_outputs):
                receipt = self.worker.collect(spec.job_id)
                receipts.append(receipt)
                if receipt["status"] != "succeeded":
                    target = (RunStatus.INFRA_FAILED if receipt["status"] == "infrastructure_failed"
                              else RunStatus.IMPLEMENTATION_FAILED)
                    self.store.runs.set_status(run_id, target)
                    ledger.settle(reservation["reservation_id"], 1.0)
                    raise RevisionExecutionError(
                        revision_id, receipt["status"], receipt.get("error_type"))
                predictions_dir = self.run_dir / "predictions" / run_id
                predictions_dir.mkdir(parents=True, exist_ok=True)
                output_path = predictions_dir / pack.invocation().prediction_name(seed)
                self.worker.fetch_artifact(spec.job_id, output, output_path)
                predictions.append({"seed": seed, "path": str(output_path),
                                    "sha256": file_hash(output_path)})
            evaluated = self.evaluator.score_artifacts(state, "dev", run_id, predictions)
            # C1: 用独立评估的 per_seed 构造 core result 并 complete_run，让 freeze
            # 能从 core.runs 选出该候选。build_result 的 per_seed 与 evaluated 的
            # per_seed 同源（都是独立评估算出的逐种子值），与 registered-implementation
            # 路径在 _evaluate_via_worker 内调用 score() 的口径一致（同一 evaluator_id）。
            if core_prior is None and core_rid is not None:
                core_result = self.exp.build_result(
                    core_rid, core_state, config, "dev", evaluated["per_seed"],
                    core_started, True)
                self.exp.complete_run(core_rid, core_result)
            if self.store.get("run", run_id)["status"] == RunStatus.RUNNING.value:
                self.store.runs.set_status(run_id, RunStatus.SUCCEEDED)
            observation = self._record_development_observations(run_id, evaluated)
            if reservation["status"] == "reserved":
                ledger.settle(reservation["reservation_id"], 1.0)
            return {"status": "succeeded", "revision": revision,
                    "receipts": receipts,
                    "observation": observation}
        except Exception as error:
            current = self.store.get("run", run_id)
            if current and current["status"] == RunStatus.RUNNING.value:
                self.store.runs.set_status(run_id, RunStatus.IMPLEMENTATION_FAILED)
            # C1: core.runs 也要从 running 推进到 failed，否则后续 register_run 会
            # 以「已有运行中的实验」拒绝。fail_run 是 idempotent 的：rid 不存在时
            # sqlite UPDATE 影响 0 行，不抛错。core_rid 为 None 表示 register_run
            # 本身就失败（已回滚，无 running 留下），跳过。
            if core_rid is not None and core_prior is None:
                try:
                    self.exp.fail_run(core_rid, type(error).__name__)
                except Exception:
                    pass
            latest = ledger.reserve(self.manifest["family_id"], run_id, 1.0)
            if latest["status"] == "reserved":
                ledger.settle(latest["reservation_id"], 1.0)
            raise
        finally:
            ledger.close()

    def _implement_with_repair(self, hypothesis_id):
        """Repair one worker implementation failure; never retry integrity/infra errors."""
        try:
            return self.implement(hypothesis_id)
        except RevisionExecutionError as error:
            if error.status != "implementation_failed":
                raise
            context = build_context(self.store, self.manifest["study_id"], self.manifest)
            if context["budget"]["available"] < 1:
                raise
            return self.implement(hypothesis_id, parent_revision_id=error.revision_id)

    def _reflect_observation(self, baseline, observation):
        from .reflection import build_reflection_context, parse_reflection
        reflection_id = "REF-" + digest({"study_id": self.manifest["study_id"],
                                         "observation_id": observation["observation_id"]})[:18]
        existing = self.store.get("reflection", reflection_id)
        if existing:
            return existing
        context = build_context(self.store, self.manifest["study_id"], self.manifest)
        reflection_context = build_reflection_context(
            context, baseline, observation, observation["hypothesis_id"])
        result = parse_reflection(self.policy.reflect(reflection_context), reflection_context)
        return self.store.apply_reflection(
            reflection_id, self.manifest["study_id"], result,
            context["study"]["state_version"],
            actor=getattr(self.policy, "name", type(self.policy).__name__),
            model=getattr(self.policy, "model", None))

    def _resume_reflections(self, baseline):
        """Complete analyses interrupted after scoring, before choosing new work."""
        candidates = set(self.manifest["candidate_hypothesis_ids"])
        for observation in self.store.list("observation"):
            if observation.get("scope") != "dev" or observation["hypothesis_id"] not in candidates:
                continue
            hypothesis = self.store.get("hypothesis", observation["hypothesis_id"])
            if hypothesis["status"] == HypothesisStatus.UNTESTED.value:
                delta = (baseline["value"] - observation["value"]
                         if self.manifest["metric"]["direction"] == "min"
                         else observation["value"] - baseline["value"])
                threshold = self.manifest["min_meaningful_effect"]
                target = (HypothesisStatus.SUPPORTED_IN_SCOPE if delta >= threshold else
                          HypothesisStatus.CONTRADICTED_IN_SCOPE if delta <= -threshold else
                          HypothesisStatus.INCONCLUSIVE)
                self.store.set_hypothesis_status(
                    hypothesis["hypothesis_id"], hypothesis["version"], target)
            self._reflect_observation(baseline, observation)

    def _reflection_terminal(self):
        context = build_context(self.store, self.manifest["study_id"], self.manifest)
        if not context["reflections"]:
            return False
        latest = context["reflections"][-1]
        if latest["action"] in {STOP, CONCLUDE_SCOPE_BOUNDARY}:
            self.store.set_study_status(self.manifest["study_id"],
                StudyStatus.BUDGET_EXHAUSTED if context["budget"]["available"] < 1
                else StudyStatus.CONCLUDED)
            return True
        return latest["action"] in {REQUEST_CONFIRMATION,
                                    REQUEST_SCOPE_BOUNDARY_CONFIRMATION}

    def _require_sandbox_backend(self):
        """没有沙箱后端时立刻给出可行路径，而不是让它在 job 里变成基础设施失败。

        沙箱后端注册表（``popper/sandbox/__init__.py``）已登记 Windows 低完整性
        （``win_lowil``）与 Linux bubblewrap（``linux_bwrap``，需 bwrap 能建非特权命名空间）；
        macOS seatbelt 尚未实现。两者都不可用的平台上 ``--sandbox`` 必须显式指出
        ``--trusted-local`` 人工路径——否则用户会被「自主代码只能用 --sandbox」和
        「本平台没有沙箱」同时堵死。具体措辞由 `sandbox.no_backend_message()` 统一提供。
        """
        if sandbox.available():
            return
        raise ProtocolError(sandbox.no_backend_message()
                            + "（--autonomous-code 依赖沙箱，在本平台不可用；"
                              "见 docs/技术方案.md §2.2）")

    def run(self, trusted_local=False, sandboxed=False, max_steps=None, auto_confirm=False,
            autonomous_code=False):
        if trusted_local == sandboxed:
            raise ProtocolError("research run 必须且只能选择 --trusted-local 或 --sandbox")
        if autonomous_code and not sandboxed:
            raise ProtocolError("自主生成代码只能使用 --sandbox，不能 trusted-local 执行")
        if sandboxed:
            self._require_sandbox_backend()
        if autonomous_code and self.harness is None:
            raise ProtocolError("--autonomous-code 需要支持提案的模型策略或 --harness 接入")
        if autonomous_code and auto_confirm:
            raise ProtocolError("自主代码开发与最终确认必须分离；当前版本不允许自动消费确认集")
        if auto_confirm and self.manifest.get("external_confirmation"):
            raise ProtocolError("独立确认需先 prepare-confirmation，再导入已签名回执")
        if max_steps is not None and (type(max_steps) is not int or max_steps < 0):
            raise ProtocolError("max_steps 必须是非负整数")
        self._verify_inputs()
        self._require_scoring_binding()
        before = self.status()
        if self._external_is_frozen():
            return before
        core_phase = self.exp.state()["phase"]
        if core_phase in {"confirming", "confirmation_failed"}:
            return before
        if core_phase == "completed":
            # Once test access was consumed, only finalize from saved results.
            # Never re-enter policy selection or reflect on confirmation feedback.
            return self.confirm(trusted_local, sandboxed) if auto_confirm else before
        if before["phase"] == "ready_for_confirmation":
            return self.confirm(trusted_local, sandboxed) if auto_confirm else before
        study = self.store.get("study", self.manifest["study_id"])
        if study["status"] != StudyStatus.ACTIVE.value:
            return self.status()
        control_id = self.manifest["control_hypothesis_id"]
        control_design = self._current_design_id(control_id)
        baseline = self._observation_for_design(control_design)
        if baseline is None:
            baseline = self._execute_development(
                control_id,
                ActionProposal(RUN_EXPERIMENT, "先执行预注册基线，建立方向统一效应的控制值。",
                               control_id, source="research_controller"),
                trusted_local, sandboxed)
            self.store.set_hypothesis_status(control_id, 1, HypothesisStatus.INCONCLUSIVE)

        steps = 0
        if hasattr(self.policy, "reflect"):
            self._resume_reflections(baseline)
            if self._reflection_terminal():
                status = self.status()
                return (self.confirm(trusted_local, sandboxed)
                        if auto_confirm and status["phase"] == "ready_for_confirmation" else status)
        while max_steps is None or steps < max_steps:
            context = build_context(self.store, self.manifest["study_id"], self.manifest)
            latest = context["reflections"][-1] if context["reflections"] else None
            pending = next((candidate for candidate in context["candidates"]
                            if latest and latest["action"] == ADD_CONTROL
                            and candidate["hypothesis_id"] == latest["next_hypothesis_id"]
                            and candidate["status"] == "untested"), None)
            proposal = (ActionProposal(RUN_EXPERIMENT, latest["rationale"],
                                       pending["hypothesis_id"], source=latest["actor"],
                                       model=latest.get("model")) if pending else self.policy.choose(context))
            if proposal.kind == REQUEST_CONFIRMATION:
                self._record(proposal)
                return self.confirm(trusted_local, sandboxed) if auto_confirm else self.status()
            if proposal.kind == STOP:
                self._record(proposal)
                target = (StudyStatus.BUDGET_EXHAUSTED
                          if context["budget"]["available"] < 1 else StudyStatus.CONCLUDED)
                self.store.set_study_status(self.manifest["study_id"], target)
                return self.status()
            if proposal.kind != RUN_EXPERIMENT:
                raise ProtocolError(f"策略在选择阶段返回非法动作: {proposal.kind}")
            hypothesis_id = proposal.hypothesis_id
            try:
                if autonomous_code:
                    observation = self._implement_with_repair(hypothesis_id)["observation"]
                else:
                    observation = self._execute_development(
                        hypothesis_id, proposal, trusted_local, sandboxed)
            except ProtocolError as error:
                self.store.set_hypothesis_status(
                    hypothesis_id, self.store.get("hypothesis", hypothesis_id)["version"],
                    HypothesisStatus.INCONCLUSIVE)
                self._record(ActionProposal(
                    ADD_CONTROL, f"实现或基础设施失败，不把它解释为科学反证：{type(error).__name__}",
                    hypothesis_id, source="research_controller"))
                steps += 1
                continue
            direction = self.manifest["metric"]["direction"]
            delta = (baseline["value"] - observation["value"] if direction == "min"
                     else observation["value"] - baseline["value"])
            threshold = self.manifest["min_meaningful_effect"]
            if delta >= threshold:
                target = HypothesisStatus.SUPPORTED_IN_SCOPE
            elif delta <= -threshold:
                target = HypothesisStatus.CONTRADICTED_IN_SCOPE
            else:
                target = HypothesisStatus.INCONCLUSIVE
            self.store.set_hypothesis_status(
                hypothesis_id, self.store.get("hypothesis", hypothesis_id)["version"], target)
            if hasattr(self.policy, "reflect"):
                self._reflect_observation(baseline, observation)
                steps += 1
                if self._reflection_terminal():
                    status = self.status()
                    return (self.confirm(trusted_local, sandboxed)
                            if auto_confirm and status["phase"] == "ready_for_confirmation" else status)
                continue
            remaining = any(h["status"] == "untested" for h in
                            build_context(self.store, self.manifest["study_id"],
                                          self.manifest)["candidates"])
            analysis = self.policy.after_observation(
                hypothesis_id, delta, threshold, remaining)
            self._record(analysis,
                         observation_refs=(baseline["observation_id"],
                                           observation["observation_id"]))
            steps += 1
            if analysis.kind == REQUEST_CONFIRMATION:
                return self.confirm(trusted_local, sandboxed) if auto_confirm else self.status()
        return self.status()

    def confirm(self, trusted_local=False, sandboxed=False):
        if trusted_local == sandboxed:
            raise ProtocolError("research confirm 必须且只能选择 --trusted-local 或 --sandbox")
        if sandboxed:
            self._require_sandbox_backend()
        self._require_scoring_binding()
        if self.manifest.get("external_confirmation"):
            raise ProtocolError("该研究已登记独立确认服务，不能用本地测试替代；请 prepare-confirmation")
        current_status = self.status()
        if current_status["phase"] == StudyStatus.CONCLUDED.value:
            return current_status
        if (current_status["decisions"] and current_status["decisions"][-1]["action"]
                == REQUEST_SCOPE_BOUNDARY_CONFIRMATION):
            raise ProtocolError("范围边界结论必须由预登记切片的独立签名保留集确认")
        selected = next((c for c in current_status["candidates"]
                         if c["status"] == HypothesisStatus.SUPPORTED_IN_SCOPE.value), None)
        core_state = self.exp.state()
        if selected is None and core_state["phase"] == "completed":
            # A failed final status write may follow the candidate's transition
            # to inconclusive. Recover only the candidate already frozen and run.
            confirmation_runs = self.store.list("run")
            selected = next((c for c in current_status["candidates"]
                if self.manifest["configs"][c["hypothesis_id"]] == core_state["selected"]
                and any(r["design_id"] == self._current_design_id(c["hypothesis_id"])
                        and r["run_id"].startswith(f"RUN-confirm-candidate-{c['hypothesis_id']}")
                        for r in confirmation_runs)), None)
        if selected is None:
            raise ProtocolError("没有达到开发阈值的假设，不能消费最终确认集")
        selected_observation = self._observation_for_design(
            self._current_design_id(selected["hypothesis_id"]), "dev")
        # 若 selected 候选的 dev observation 来自生成代码 revision，则在 test split
        # 上必须经 Worker 执行该 revision（holdout 封读窗口覆盖整段执行），
        # 而不能用 evaluate() 跑原始注册代码冒充——那会让「测试结果」对应的代码
        # 与「dev 阶段冻结的候选」不一致。本块定位 revision manifest，供后续路由。
        selected_revision = None
        for manifest_path in (self.run_dir / "revisions").glob("REV-*/revision.json"):
            revision = read_json(manifest_path)
            revision_run = "RUN-" + digest(
                {"revision_id": revision["revision_id"], "split": "dev"})[:24]
            if selected_observation and selected_observation["run_id"] == revision_run:
                # verify 重算文件 SHA-256 与 manifest 摘要，确保 revision 自 dev
                # 阶段以来未被篡改；任何篡改在这里失败，不会带到 test 集执行。
                selected_revision = self.revisions.verify(revision["revision_id"])
                break
        state = self._verify_inputs()
        control_id = self.manifest["control_hypothesis_id"]
        pairs = [("control", control_id), ("candidate", selected["hypothesis_id"])]
        ledger = self.store.budget(self.manifest["study_id"])
        reservations = {}

        def release_unstarted():
            for run_id, (reservation, _) in reservations.items():
                current = self.store.get("run", run_id)
                if current and current["status"] in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
                    self.store.runs.set_status(run_id, RunStatus.CANCELLED)
                if reservation["status"] == "reserved":
                    ledger.settle(reservation["reservation_id"], 0.0)

        def finish_failed_confirmation():
            # The core commits test_consumed before launching either test. Only
            # searching/frozen establish that it is safe to refund this attempt.
            consumed = self.exp.state()["phase"] not in {"searching", "frozen"}
            completed_configs = ({canonical(result["config"])
                                  for result in self.exp.results("test")} if consumed else set())
            for run_id, (reservation, hypothesis_id) in reservations.items():
                current = self.store.get("run", run_id)
                executed = canonical(self.manifest["configs"][hypothesis_id]) in completed_configs
                if current and current["status"] == RunStatus.QUEUED.value:
                    self.store.runs.set_status(run_id, RunStatus.CANCELLED)
                elif current and current["status"] == RunStatus.RUNNING.value:
                    # A scorer failure does not undo a completed core execution.
                    # Keep that Run succeeded so recovery can score its saved
                    # predictions without running the held-out experiment again.
                    target = (RunStatus.SUCCEEDED if consumed and executed else
                              RunStatus.INFRA_FAILED if consumed else RunStatus.CANCELLED)
                    self.store.runs.set_status(run_id, target)
                if reservation["status"] == "reserved":
                    ledger.settle(reservation["reservation_id"], 1.0 if consumed else 0.0)

        try:
            phase = self.exp.state()["phase"]
            if phase == "confirming":
                raise ProtocolError("底层确认仍在执行或已中断；请先显式 recover，不能再次消费测试")
            if phase == "confirmation_failed":
                # Reconcile outstanding records left by an older controller or
                # an interrupted invocation, while refusing another test attempt.
                for run in self.store.list("run"):
                    for label, hypothesis_id in pairs:
                        prefix = f"RUN-confirm-{label}-{hypothesis_id}"
                        if run["run_id"] == prefix or run["run_id"].startswith(prefix + "-attempt-"):
                            reservation = ledger.reserve(self.manifest["family_id"], run["run_id"], 1.0)
                            reservations[run["run_id"]] = (reservation, hypothesis_id)
                raise ProtocolError("底层确认已失败并消费测试访问，禁止重复执行")

            attempt = 1
            while True:
                reservations = {}
                run_ids = {}
                retry_preparation = False
                for label, hypothesis_id in pairs:
                    suffix = f"-attempt-{attempt}" if attempt > 1 else ""
                    run_id = f"RUN-confirm-{label}-{hypothesis_id}{suffix}"
                    reservation = ledger.reserve(self.manifest["family_id"], run_id, 1.0)
                    reservations[run_id] = (reservation, hypothesis_id)
                    current = self.store.get("run", run_id)
                    if reservation["status"] == "settled" and reservation["spent"] == 0.0:
                        # A refunded item cannot be re-reserved. Use a fresh Run
                        # identity, retaining the cancelled attempt in history.
                        retry_preparation = True
                        continue
                    if current and current["status"] in {
                            RunStatus.CANCELLED.value, RunStatus.INFRA_FAILED.value,
                            RunStatus.IMPLEMENTATION_FAILED.value}:
                        if phase in {"searching", "frozen"}:
                            retry_preparation = True
                            continue
                        raise ProtocolError("确认 Run 已终态失败，不能复用其执行或预算")
                    run_ids[label] = run_id
                if not retry_preparation:
                    break
                # Inspect both identities before advancing: a crash during
                # cleanup may have refunded only the first reservation.
                release_unstarted()
                attempt += 1
            # Reserve the complete pair before creating any running records.
            for label, hypothesis_id in pairs:
                run_id = run_ids[label]
                if self.store.get("run", run_id) is None:
                    self.store.runs.register(run_id, self._current_design_id(hypothesis_id))
                if self.store.get("run", run_id)["status"] == RunStatus.QUEUED.value:
                    self.store.runs.set_status(run_id, RunStatus.RUNNING)
            phase = self.exp.state()["phase"]
            if phase == "searching":
                self.exp.freeze()
                phase = "frozen"
            if phase == "frozen":
                # begin → evaluate(baseline) + (evaluate(candidate) | Worker(candidate))
                # → finalize。candidate 经 Worker 路径的条件：其 dev observation
                # 来自生成代码 revision（见 selected_revision 捕获）。其余候选
                # （控制基线 / 未生成代码的候选）走 evaluate() 同步路径。
                # begin_confirmation 已提交 test_consumed，所以下面的 evaluate /
                # _evaluate_via_worker 看到 phase=confirming 才会接受 test split。
                core_state = self.exp.begin_confirmation(trusted_local, sandboxed)
                try:
                    baseline = self.exp.evaluate(
                        core_state["spec"]["baseline"], "test", trusted_local, sandboxed)
                    if selected_revision is not None:
                        candidate = self._evaluate_via_worker(
                            core_state["selected"],
                            self._current_design_id(selected["hypothesis_id"]),
                            selected_revision, "test", sandboxed=sandboxed)
                    else:
                        candidate = self.exp.evaluate(
                            core_state["selected"], "test", trusted_local, sandboxed)
                    self.exp.finalize_confirmation(baseline, candidate)
                except Exception:
                    # begin 之后、finalize 之前的失败（执行中断、超时、scoring 不一致）
                    # 必须把 phase 从 confirming 推进到 confirmation_failed，否则用户
                    # 只能 recover，但 recover 也只是把它转成 confirmation_failed。
                    # 与原 Experiment.confirm 同步路径的 except 行为对齐。finalize 内部
                    # 自己已经做这件事，这里只兜底执行阶段的失败。
                    self.exp.mark_confirmation_failed()
                    raise
            elif phase != "completed":
                raise ProtocolError(f"底层实验不能确认: phase={phase}")
            test_results = {canonical(r["config"]): r for r in self.exp.results("test")}
            observations = []
            for label, hypothesis_id in pairs:
                run_id = run_ids[label]
                reservation = reservations[run_id][0]
                existing = self._observation_for_design(self._current_design_id(hypothesis_id), "confirmation")
                if existing and existing["run_id"] == run_id:
                    observations.append(existing)
                    if reservation["status"] == "reserved":
                        ledger.settle(reservation["reservation_id"], 1.0)
                    continue
                result = test_results.get(canonical(self.manifest["configs"][hypothesis_id]))
                if result is None:
                    raise ProtocolError("确认结果缺少预注册基线或冻结候选")
                evaluated = self.evaluator.score_core_result(state, result)
                if abs(float(evaluated["mean"]) - float(result["mean"])) > 1e-12:
                    raise ProtocolError("确认结果与独立评估重算不一致")
                if self.store.get("run", run_id)["status"] == RunStatus.RUNNING.value:
                    self.store.runs.set_status(run_id, RunStatus.SUCCEEDED)
                observation_id = "OBS-confirm-" + digest(
                    {"run_id": run_id, "request": evaluated["request_sha256"]})[:18]
                if self.store.get("observation", observation_id) is None:
                    self.store.observations.add(Observation(
                        observation_id, run_id, self.manifest["scorer_id"],
                        evaluated["mean"], unit=self.manifest["metric"]["name"],
                        uncertainty=evaluated["std"], scope="confirmation",
                        artifact_id=evaluated["artifact_id"],
                        artifact_sha256=evaluated["artifact_sha256"], selector="mean",
                        evaluator_service_hash=evaluated["service_sha256"],
                        trust=evaluated["trust"]))
                observations.append(self.store.get("observation", observation_id))
                if reservation["status"] == "reserved":
                    ledger.settle(reservation["reservation_id"], 1.0)
            direction = self.manifest["metric"]["direction"]
            delta = (observations[0]["value"] - observations[1]["value"]
                     if direction == "min" else
                     observations[1]["value"] - observations[0]["value"])
            passed = delta >= self.manifest["min_meaningful_effect"]
            observation_refs = tuple(o["observation_id"] for o in observations)
            already_recorded = any(
                d["actor"] == "confirmation_controller" and d["action"] == STOP
                and tuple(d["observation_refs"]) == observation_refs
                for d in self.store.list("decision"))
            if not already_recorded:
                self._record(ActionProposal(
                    STOP, f"独立进程确认的方向统一效应为 {delta:.8g}；"
                          f"预注册阈值为 {self.manifest['min_meaningful_effect']:.8g}；"
                          f"结论={'支持' if passed else '证据不足'}。",
                    alternatives=("新的独立数据复现",), source="confirmation_controller"),
                    observation_refs=observation_refs)
            if not passed and selected["status"] != HypothesisStatus.INCONCLUSIVE.value:
                self.store.set_hypothesis_status(
                    selected["hypothesis_id"], selected["version"], HypothesisStatus.INCONCLUSIVE)
            self.store.set_study_status(self.manifest["study_id"], StudyStatus.CONCLUDED)
            return self.status()
        except Exception:
            finish_failed_confirmation()
            raise
        finally:
            ledger.close()

    def status(self):
        core_state = self._verify_inputs()
        context = build_context(self.store, self.manifest["study_id"], self.manifest)
        decisions = context["decisions"]
        study_status = context["study"]["status"]
        if study_status != StudyStatus.ACTIVE.value:
            phase = study_status
        elif self._external_is_frozen():
            phase = ("external_confirmation_pending" if self._external_submission_record()
                     else "external_preparation_incomplete")
        elif core_state["phase"] in {"confirming", "confirmation_failed"}:
            phase = core_state["phase"]
        elif core_state["phase"] == "completed":
            phase = "ready_for_confirmation"
        elif decisions and decisions[-1]["action"] in {
                REQUEST_CONFIRMATION, REQUEST_SCOPE_BOUNDARY_CONFIRMATION}:
            phase = "ready_for_confirmation"
        else:
            phase = "researching"
        revisions = []
        if (self.run_dir / "revisions").exists():
            for path in (self.run_dir / "revisions").glob("REV-*/revision.json"):
                if read_json(path).get("identity", {}).get("kind") != "registered_implementation_dev":
                    revisions.append(path)
        execution_gates = []
        for receipt in self.worker.list_receipts():
            if "execution_gate" in receipt:
                gate = receipt["execution_gate"] or {}
                execution_gates.append({"job_id": receipt["job_id"],
                    "revision_id": receipt["revision_id"], "status": receipt["status"],
                    "passed": gate.get("passed", False), "reason": gate.get("reason"),
                    "coverage": gate.get("coverage"),
                    "uncovered_files": gate.get("uncovered_files", []),
                    "trust": "in_process_trace", "scientific_mechanism_verified": False})
        return {"schema_version": "1.0", "phase": phase,
                "study": context["study"], "candidates": context["candidates"],
                "observations": context["observations"],
                "decisions": decisions, "budget": context["budget"],
                "reflections": context["reflections"],
                "integrity": self.store.verify(),
                "capability_mode": self.manifest["capability_mode"],
                "evaluation_trust": "separate_process_same_account",
                "code_revisions": len(revisions),
                "execution_gates": execution_gates,
                "generated_code_execution": "sandbox_only"}

    def prepare_external_confirmation(self):
        """Freeze the selected revision and reserve the original family budget."""
        from .confirmation_bundle import export_confirmation_bundle, verify_bundle
        self._verify_inputs()
        self._require_scoring_binding()
        enrollment = self.manifest.get("external_confirmation")
        if enrollment is None:
            raise ProtocolError("独立确认合同必须在研究初始化时登记")
        target = self._external_bundle_dir()
        if target.exists():
            submission, _ = verify_bundle(target, enrollment["pinned_public_key"])
        else:
            ledger = self.store.budget(self.manifest["study_id"])
            try:
                if ledger.balance(self.manifest["family_id"])["available"] < 2:
                    raise ProtocolError("独立确认预算不足，不能冻结提交")
            finally:
                ledger.close()
            submission = export_confirmation_bundle(
                self, enrollment["contract"], enrollment["pinned_public_key"], target)
        identity = submission["identity"]
        if (identity["study_id"] != self.manifest["study_id"]
                or identity["source_manifest_sha256"] != file_hash(self.manifest_path)
                or identity["contract_sha256"] != digest(enrollment["contract"])):
            raise ProtocolError("确认提交与本研究登记合同不一致")
        existing = self._external_submission_record()
        if existing is not None:
            if existing["submission_sha256"] != digest(submission):
                raise ProtocolError("不能替换已冻结确认提交")
            return {"bundle_dir": str(target), "submission": submission, "status": "frozen"}
        ledger = self.store.budget(self.manifest["study_id"])
        try:
            # 该预留跨研究终结存活，直到外部权威回执到达；标记 external 以免启动对账误回收。
            reservation = ledger.reserve(self.manifest["family_id"],
                                         "EXT-" + submission["submission_id"], 2.0,
                                         kind="external")
            record = {"submission_id": submission["submission_id"], "submission_sha256": digest(submission),
                      "reservation_id": reservation["reservation_id"]}
            with self.store.terminal_confirmation_writes():
                self.store.add_decision(Decision(
                    decision_id="DEC-freeze-" + submission["submission_id"],
                    study_id=self.manifest["study_id"],
                    state_version=self.store.get("study", self.manifest["study_id"])["state_version"],
                    action="freeze_external_confirmation", rationale=canonical(record),
                    observation_refs=tuple(identity.get("dev_evidence_refs",
                                                        [identity["dev_observation_id"]])),
                    budget_request=2.0,
                    actor="external_confirmation_controller"))
        finally:
            ledger.close()
        return {"bundle_dir": str(target), "submission": submission, "status": "frozen"}

    def accept_external_confirmation(self, signed_result):
        """Verify the pinned authority and finish only from its frozen receipt."""
        from .confirmation_bundle import verify_bundle
        from .confirmation_contracts import validate_result
        self._verify_inputs()
        self._require_scoring_binding()
        record = self._external_submission_record()
        if record is None:
            raise ProtocolError("尚未冻结独立确认提交")
        enrollment = self.manifest["external_confirmation"]
        submission, contract = verify_bundle(self._external_bundle_dir(), enrollment["pinned_public_key"])
        result = validate_result(signed_result, enrollment["contract"], submission,
                                 enrollment["pinned_public_key"])
        receipt_path = self.run_dir / "external-confirmation" / "result.json"
        try:
            with receipt_path.open("x", encoding="utf-8") as stream:
                stream.write(canonical(signed_result))
        except FileExistsError:
            if read_json(receipt_path) != signed_result:
                raise ProtocolError("不能替换已接受的独立确认回执")
        ledger = self.store.budget(self.manifest["study_id"])
        try:
            ledger.settle(record["reservation_id"], 2.0)
        finally:
            ledger.close()
        posthoc = self.store.get("study", self.manifest["study_id"])["status"] != StudyStatus.ACTIVE.value
        identity = submission["identity"]
        roles = [("control", self.manifest["control_hypothesis_id"]),
                 ("candidate", identity["hypothesis_id"])]
        refs = []
        role_runs = {}
        with self.store.terminal_confirmation_writes():
            for role, hypothesis_id in roles:
                run_id = "RUN-ext-" + digest({"submission": submission["submission_id"], "role": role})[:24]
                design_id = (self._current_design_id(hypothesis_id) if role == "control" else identity["design_id"])
                run = self.store.get("run", run_id)
                if run is None:
                    self.store.runs.register(run_id, design_id)
                    run = self.store.get("run", run_id)
                if run["status"] == RunStatus.QUEUED.value:
                    self.store.runs.set_status(run_id, RunStatus.RUNNING)
                if self.store.get("run", run_id)["status"] == RunStatus.RUNNING.value:
                    self.store.runs.set_status(run_id, RunStatus.SUCCEEDED if result["status"] == "succeeded"
                                              else RunStatus.INFRA_FAILED)
                if result["status"] == "succeeded":
                    observation_id = "OBS-ext-" + digest({"run_id": run_id, "receipt": digest(signed_result)})[:24]
                    if self.store.get("observation", observation_id) is None:
                        self.store.observations.add(Observation(
                            observation_id, run_id, self.manifest["scorer_id"], result[role]["mean"],
                            unit=self.manifest["metric"]["name"], uncertainty=result[role]["std"],
                            scope="confirmation", artifact_id=str(receipt_path),
                            artifact_sha256=file_hash(receipt_path), selector=f"payload.{role}.mean",
                            evaluator_service_hash=result["service_code_sha256"], trust=result["trust"]))
                    refs.append(observation_id)
                    role_runs[role] = (run_id, hypothesis_id)
            if result["status"] == "succeeded" and result.get("confirmation_kind") == "scope_boundary":
                for index, registered in enumerate(contract["analysis_slices"]):
                    slice_id = registered["slice_id"]
                    for role, _ in roles:
                        run_id, hypothesis_id = role_runs[role]
                        item = result[role]["slices"][index]
                        if item["slice_id"] != slice_id:
                            raise ProtocolError("签名确认切片顺序与冻结合同不一致")
                        observation_id = "OBS-ext-slice-" + digest({
                            "run_id": run_id, "receipt": digest(signed_result),
                            "slice_id": slice_id})[:24]
                        if self.store.get("observation", observation_id) is None:
                            self.store.observations.add(Observation(
                                observation_id, run_id, self.manifest["scorer_id"], item["mean"],
                                unit=self.manifest["metric"]["name"], uncertainty=item["std"],
                                scope=f"confirmation:slice:{slice_id}", artifact_id=str(receipt_path),
                                artifact_sha256=file_hash(receipt_path),
                                selector=f"payload.{role}.slices.{index}.mean",
                                evaluator_service_hash=result["service_code_sha256"],
                                trust=result["trust"]))
                        refs.append(observation_id)
            boundary_passed = (result["status"] == "succeeded" and result["passed"]
                               and result.get("confirmation_kind") == "scope_boundary")
            final_action = CONCLUDE_SCOPE_BOUNDARY if boundary_passed else STOP
            decision_id = "DEC-result-" + submission["submission_id"]
            if self.store.get("decision", decision_id) is None:
                self.store.add_decision(Decision(
                    decision_id, self.manifest["study_id"],
                    self.store.get("study", self.manifest["study_id"])["state_version"],
                    observation_refs=tuple(refs), action=final_action,
                    rationale=canonical({"external_receipt_sha256": digest(signed_result),
                                         "status": result["status"], "passed": result["passed"],
                                         "effect": result["effect"],
                                         "confirmation_kind": result.get("confirmation_kind",
                                                                         "positive_effect"),
                                         "trust": result["trust"]}),
                    actor="external_confirmation_controller"))
            if (result["status"] == "succeeded"
                    and (not result["passed"] or result.get("confirmation_kind") == "scope_boundary")):
                selected = self.store.get("hypothesis", identity["hypothesis_id"])
                if selected["status"] != HypothesisStatus.INCONCLUSIVE.value:
                    self.store.set_hypothesis_status(identity["hypothesis_id"], identity["hypothesis_version"],
                                                     HypothesisStatus.INCONCLUSIVE)
        if not posthoc:
            self.store.set_study_status(self.manifest["study_id"],
                                        StudyStatus.CONCLUDED if result["status"] == "succeeded"
                                        else StudyStatus.BLOCKED_EXTERNAL)
        return self.status()


def _design_id(hypothesis_id, version=1):
    return "D-" + hypothesis_id.removeprefix("H-") + (f"-v{version}" if version > 1 else "")


def _default_hypothesis(config, metric):
    return {"mechanism": f"预注册干预 {canonical(config)} 可能改变 {metric['name']}",
            "applicability": "当前固定训练与开发划分",
            "predictions": [f"相对基线按 {metric['direction']} 方向改善 {metric['name']}"],
            "falsification": ["方向统一效应未达到预注册最小有意义阈值"],
            "alternatives": ["差异由训练随机性或配置交互解释"]}


def _create_hypothesis_and_design(store, manifest, hypothesis_id, content,
                                  is_control=False):
    hypothesis = Hypothesis(
        hypothesis_id, manifest["study_id"], 1, None,
        str(content["mechanism"]), str(content["applicability"]),
        tuple(content.get("predictions", [])), tuple(content.get("falsification", [])),
        tuple(content.get("alternatives", [])))
    store.add_hypothesis(hypothesis)
    config = manifest["configs"][hypothesis_id]
    design = ExperimentDesign(
        _design_id(hypothesis_id), hypothesis_id,
        interventions=(canonical(config),), control=canonical(
            manifest["configs"][manifest["control_hypothesis_id"]]),
        baseline=canonical(manifest["configs"][manifest["control_hypothesis_id"]]),
        metric=manifest["metric"]["name"], scorer_id=manifest["scorer_id"],
        metric_direction=manifest["metric"]["direction"],
        analysis_unit="training_seed_descriptive",
        splits=("dev", "confirmation") if not is_control else ("dev", "confirmation"),
        seeds=tuple(manifest["seeds"]),
        min_meaningful_effect=manifest["min_meaningful_effect"],
        status=DesignStatus.DRAFT)
    store.create_design(design)
    store.freeze_design(design.design_id)
