"""Freeze the actually evaluated generated code without exporting holdout labels."""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path, PurePosixPath

from ..core import (ProtocolError, dataset, digest, evaluator_pack, file_hash, model_inputs,
                    read_json, write_json)
from .confirmation_contracts import validate_contract, validate_submission
from .evaluation_service import _validate_response, scoring_code_hash
from .execution import assess_execution
from .workers import job_invocation


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _plain(path):
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
        raise ProtocolError("确认包不能包含符号链接或目录重解析点")


def _plain_ancestors(path):
    for item in (path, *path.parents):
        _plain(item)


def _relative(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
            or PurePosixPath(name).as_posix() != name):
        raise ProtocolError("确认包文件路径必须是规范安全相对路径")
    return name


def _file(root, name):
    path = root / _relative(name)
    _plain_ancestors(path)
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ProtocolError("确认包文件缺失或路径越界")
    return path


def _inventory(root):
    _plain_ancestors(root)
    if not root.is_dir():
        raise ProtocolError("确认包目录不存在")
    files, directories, pending = set(), set(), [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            _plain(path)
            name = path.relative_to(root).as_posix()
            if path.is_dir():
                directories.add(name)
                pending.append(path)
            elif path.is_file():
                files.add(name)
            else:
                raise ProtocolError("确认包只允许普通文件和目录")
    return files, directories


def _revision_matches(revision, identity):
    candidate = identity["candidate"]
    if (revision.get("status") != "immutable"
            or revision.get("revision_id") != candidate["revision_id"]
            or "REV-" + digest(revision.get("identity"))[:24] != candidate["revision_id"]
            or revision.get("files") != candidate["files"]
            or revision.get("entrypoint") != candidate["entrypoint"]
            or revision["identity"].get("hypothesis_id") != identity["hypothesis_id"]
            or revision["identity"].get("design_id") != identity["design_id"]
            or revision["identity"].get("execution_changes") != candidate["execution_changes"]):
        raise ProtocolError("确认包候选与已冻结 CodeRevision 不一致")


def verify_bundle(bundle_dir, pinned_public_key):
    """Verify the signed contract, frozen identity and exact byte-level file set."""
    root = Path(bundle_dir).absolute()
    try:
        actual_files, actual_dirs = _inventory(root)
        manifest = read_json(_file(root, "bundle.json"))
        envelope = read_json(_file(root, "contract.json"))
        contract = validate_contract(envelope, pinned_public_key)
        identity = validate_submission(manifest, contract)
        if identity["contract_sha256"] != digest(envelope):
            raise ProtocolError("确认包未绑定提供的签名 contract")
        generated = (identity["candidate"].get("implementation_mode")
                     != "registered_implementation")
        expected = {"bundle.json", "contract.json", "train.json"}
        if generated:
            expected.add("candidate/revision.json")
        for role in ("control", "candidate"):
            for name, expected_hash in identity[role]["files"].items():
                if PurePosixPath(_relative(name)).suffix != ".py":
                    raise ProtocolError("确认包代码清单只允许 Python 文件")
                relative = f"{role}/code/{name}"
                expected.add(relative)
                if file_hash(_file(root, relative)) != expected_hash:
                    raise ProtocolError("确认包代码 SHA-256 已变化")
        expected_dirs = {parent.as_posix() for name in expected
                         for parent in PurePosixPath(name).parents if parent.as_posix() != "."}
        if actual_files != expected or actual_dirs != expected_dirs:
            raise ProtocolError("确认包文件或目录集合不匹配；不允许额外数据")
        if file_hash(_file(root, "train.json")) != identity["train_sha256"]:
            raise ProtocolError("确认包训练数据 SHA-256 已变化")
        if generated:
            revision_path = _file(root, "candidate/revision.json")
            if file_hash(revision_path) != identity["candidate"]["revision_manifest_sha256"]:
                raise ProtocolError("确认包 revision manifest SHA-256 已变化")
            _revision_matches(read_json(revision_path), identity)
        return manifest, contract
    except ProtocolError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ProtocolError(f"确认包验证失败: {type(error).__name__}") from None


def _evaluated_request(controller, observation, state):
    response_path = Path(observation["artifact_id"])
    _plain_ancestors(response_path)
    if file_hash(response_path) != observation["artifact_sha256"]:
        raise ProtocolError("开发观察评估回执已变化")
    request = read_json(response_path.parent / "request.json")
    response = read_json(response_path)
    _validate_response(response, request)
    if (request["dataset_sha256"] != state["input_hashes"][state["spec"]["dev"]]
            or Path(request["dataset"]).resolve() != (controller.project / state["spec"]["dev"]).resolve()
            or request["evaluator_id"] != state["evaluator_id"]
            or request["evaluator_hash"] != state["evaluator_hash"]
            or request["expected_seeds"] != state["spec"]["seeds"]
            or request["scoring_code_sha256"] != controller.manifest["scoring_code_sha256"]
            or response["mean"] != observation["value"]
            or response["std"] != observation["uncertainty"]):
        raise ProtocolError("开发观察未绑定当前预注册数据和计分实现")
    return request


def _execution_evidence(controller, revision, observation, request, state):
    revision_id = revision["revision_id"]
    revision_sha = file_hash(controller.revisions.path(revision_id) / "revision.json")
    input_root = controller.run_dir / "revision-inputs" / revision_id
    dev_input_path = _file(input_root, "inputs.json")
    config_path = _file(input_root, "config.json")
    if (read_json(dev_input_path) != model_inputs(
            dataset(controller.project / state["spec"]["dev"], state["evaluator_id"]), state["evaluator_id"])
            or read_json(config_path) != controller.manifest["configs"][observation["hypothesis_id"]]):
        raise ProtocolError("候选开发输入或实际干预配置与冻结研究不一致")
    expected_inputs = [
        {"source": str(controller.project / state["spec"]["train"]), "target": "train.json",
         "sha256": state["input_hashes"][state["spec"]["train"]], "role": "training_data"},
        {"source": str(dev_input_path), "target": "inputs.json", "sha256": file_hash(dev_input_path),
         "role": "development_features_without_labels"},
        {"source": str(config_path), "target": "config.json", "sha256": file_hash(config_path),
         "role": "frozen_intervention"},
    ]
    expected_predictions = []
    pack = evaluator_pack(state["evaluator_id"])
    for seed in state["spec"]["seeds"]:
        key = f"{observation['run_id']}:seed:{seed}:execution-v1"
        job_id = "JOB-" + digest({"idempotency_key": key})[:24]
        job = controller.worker.read_manifest(job_id)
        receipt = controller.worker.collect(job_id)
        spec = job["spec"]
        expected_args, expected_outputs = job_invocation(pack, seed)
        output = expected_outputs[0]
        if (job.get("job_id") != job_id or job["status"] != "succeeded"
                or spec.get("idempotency_key") != key or spec.get("revision_id") != revision_id
                or spec.get("revision_manifest_sha256") != revision_sha
                or spec.get("design_id") != observation["design_id"]
                or spec.get("entrypoint") != revision["entrypoint"]
                or spec.get("require_edit_coverage") is not True
                or spec.get("inputs") != expected_inputs
                or spec.get("outputs") != list(expected_outputs)
                or spec.get("args") != list(expected_args)
                or receipt.get("job_id") != job_id or receipt.get("status") != "succeeded"
                or receipt.get("revision_id") != revision_id
                or receipt.get("revision_manifest_sha256") != revision_sha):
            raise ProtocolError("候选开发执行未完整绑定所有预注册 seed/revision")
        for item in expected_inputs:
            copied = controller.worker.read_workspace_file(job_id, "inputs/" + item["target"])
            if _sha256(copied) != item["sha256"]:
                raise ProtocolError("候选实际使用的开发输入副本已变化")
        trace_name = "outputs/_popper_execution.json"
        trace_bytes = controller.worker.read_workspace_file(job_id, trace_name)
        if receipt["artifacts"].get(trace_name) != _sha256(trace_bytes):
            raise ProtocolError("候选执行轨迹未绑定 worker 回执")
        gate = assess_execution(revision["identity"]["execution_changes"], json.loads(trace_bytes))
        if not gate["passed"] or receipt.get("execution_gate") != gate:
            raise ProtocolError("候选必须通过完整的生成代码执行检查")
        prediction_bytes = controller.worker.read_workspace_file(job_id, output)
        prediction_path = (controller.run_dir / "predictions" / observation["run_id"]
                           / pack.invocation().prediction_name(seed))
        expected_predictions.append({"seed": seed, "path": str(prediction_path),
                                     "sha256": _sha256(prediction_bytes)})
    if request["request_id"] != observation["run_id"] or request["predictions"] != expected_predictions:
        raise ProtocolError("开发计分预测制品未绑定选中 revision 的完整 worker 输出")


def _registered_execution_evidence(controller, observation, request, state):
    """Bind a no-edit candidate to the exact registered core execution."""
    receipt_path = controller.run_dir / "receipts" / f"{observation['run_id']}.json"
    receipt = read_json(_file(receipt_path.parent, receipt_path.name))
    result = receipt.get("core_result")
    if (not isinstance(result, dict) or receipt.get("evaluation", {}).get("request_sha256")
            != digest(request)):
        raise ProtocolError("Registered implementation receipt is incomplete")
    registered = next((row for row in controller.exp.results("dev")
                       if row.get("run_id") == result.get("run_id")), None)
    if registered != result or request["request_id"] != result["run_id"]:
        raise ProtocolError("Registered implementation result is not in the core ledger")
    run_root = controller.project / ".popper" / "runs" / result["run_id"]
    invocation = evaluator_pack(state["evaluator_id"]).invocation()
    expected_predictions = [{"seed": point["seed"],
                             "path": str(run_root / invocation.prediction_name(point["seed"])),
                             "sha256": file_hash(run_root / invocation.prediction_name(point["seed"]))}
                            for point in result["per_seed"]]
    if request["predictions"] != expected_predictions:
        raise ProtocolError("Registered implementation predictions differ from the scored artifacts")


def _posthoc_confirmation_target(source, status):
    """Select one deterministically confirmable candidate after termination.

    A comparator may reach ``concluded``/``budget_exhausted`` without emitting a
    ``request_confirmation`` decision (evidence-blind fixed plans, or boundary
    effects whose global never crossed the threshold). Confirmation is an
    independent post-hoc audit, so the target is derived from recorded candidate
    states rather than a policy decision. When several candidates qualify, the
    strongest directional development effect is chosen so the holdout judge can
    reproduce the same candidate's evidence without exposing held-out labels.
    """
    control_id = source["control_hypothesis_id"]
    threshold = float(source["min_meaningful_effect"])
    direction = source["metric"]["direction"]
    candidates = [c for c in status["candidates"]
                  if c["hypothesis_id"] != control_id]
    by_hypothesis = {}
    for row in status["observations"]:
        if row["scope"] == "dev" or row["scope"].startswith("dev:slice:"):
            by_hypothesis.setdefault(row["hypothesis_id"], {})[row["scope"]] = row["value"]
    control = by_hypothesis.get(control_id, {})
    if "dev" not in control:
        raise ProtocolError("缺少已运行的基线开发观察")
    slice_ids = [item["slice_id"] for item in source.get("analysis_slices", [])]

    def _effect(raw):
        return -raw if direction == "min" else raw

    best_supported = None
    best_boundary = None
    for candidate in candidates:
        observed = by_hypothesis.get(candidate["hypothesis_id"], {})
        if "dev" not in observed:
            continue
        global_effect = _effect(observed["dev"] - control["dev"])
        slice_effects = []
        for slice_id in slice_ids:
            scope = f"dev:slice:{slice_id}"
            if scope in control and scope in observed:
                slice_effects.append(_effect(observed[scope] - control[scope]))
        if global_effect >= threshold:
            if best_supported is None or global_effect > best_supported[1]:
                best_supported = (candidate, global_effect)
        elif (abs(global_effect) < threshold and len(slice_effects) >= 2
                and max(slice_effects) >= threshold and min(slice_effects) < threshold):
            peak = max(slice_effects)
            if best_boundary is None or peak > best_boundary[1]:
                best_boundary = (candidate, peak)
    if best_supported is not None:
        return best_supported[0], "positive_effect"
    if best_boundary is not None:
        return best_boundary[0], "scope_boundary"
    raise ProtocolError("研究终止后没有可确认的 supported 或边界形候选")


def _boundary_evidence(source, status, control_id, selected_hypothesis_id):
    slice_ids = [item["slice_id"] for item in source.get("analysis_slices", [])]
    scopes = ["dev", *[f"dev:slice:{slice_id}" for slice_id in slice_ids]]
    wanted = {(scope, hypothesis_id) for scope in scopes
              for hypothesis_id in (control_id, selected_hypothesis_id)}
    return {row["observation_id"]: row for row in status["observations"]
            if (row["scope"], row["hypothesis_id"]) in wanted}


def export_confirmation_bundle(controller, contract_envelope, pinned_public_key, output_dir):
    """Export one auditable submission; never evaluate or copy the test split."""
    output = Path(output_dir).absolute()
    staging = None
    try:
        _plain_ancestors(output)
        if output.exists():
            raise ProtocolError("确认包输出目录已存在，不能覆盖")
        controller._require_scoring_binding()
        state = controller._verify_inputs()
        status = controller.status()
        if not status["integrity"]["ok"]:
            raise ProtocolError("研究完整性校验失败，不能导出确认包")
        if state["phase"] != "searching":
            raise ProtocolError("确认包必须在消费确认集之前冻结")
        contract = validate_contract(contract_envelope, pinned_public_key)
        source = controller.manifest
        enrollment = source.get("external_confirmation")
        if (not isinstance(enrollment, dict) or enrollment.get("contract") != contract_envelope
                or enrollment.get("pinned_public_key") != pinned_public_key):
            raise ProtocolError("确认包只能使用研究开始前登记的确切合同与服务公钥")
        expected = {"train_sha256": state["input_hashes"][state["spec"]["train"]],
                    "dev_sha256": state["input_hashes"][state["spec"]["dev"]],
                    "evaluator_id": state["evaluator_id"], "evaluator_hash": state["evaluator_hash"],
                    "scoring_code_sha256": source["scoring_code_sha256"],
                    "seeds": source["seeds"], "min_effect": source["min_meaningful_effect"]}
        if contract["schema_version"] == "2.0":
            expected["analysis_slices"] = source.get("analysis_slices", [])
        if any(contract.get(key) != value for key, value in expected.items()) or source["scoring_code_sha256"] != scoring_code_hash():
            raise ProtocolError("签名确认 contract 与研究的数据/计分/seed/阈值不一致")
        decision = status["decisions"][-1] if status["decisions"] else None
        triggered = (decision is not None and decision["action"] in {
            "request_confirmation", "request_scope_boundary_confirmation"})
        if triggered:
            if status["phase"] != "ready_for_confirmation":
                raise ProtocolError("请求确认研究尚未进入 ready_for_confirmation 阶段")
            confirmation_kind = ("scope_boundary" if
                                 decision["action"] == "request_scope_boundary_confirmation"
                                 else "positive_effect")
            if confirmation_kind == "scope_boundary":
                referenced = [row for row in status["observations"]
                              if row["observation_id"] in decision["observation_refs"]
                              and row["scope"] == "dev"
                              and not row["hypothesis_id"].startswith("H-control-")]
                candidates = [row for row in status["candidates"]
                              if any(obs["hypothesis_id"] == row["hypothesis_id"]
                                     for obs in referenced)]
            else:
                candidates = [row for row in status["candidates"]
                              if row["status"] == "supported_in_scope"]
            if len(candidates) != 1:
                raise ProtocolError("确认包必须唯一绑定实际选中的候选")
            selected = candidates[0]
        else:
            if status["phase"] not in {"concluded", "budget_exhausted"}:
                raise ProtocolError("仅可从 ready_for_confirmation 或已终止研究导出确认包")
            selected, confirmation_kind = _posthoc_confirmation_target(source, status)
        observations = [row for row in status["observations"] if row["scope"] == "dev"
                        and row["design_id"] == selected["design_id"]
                        and row["hypothesis_id"] == selected["hypothesis_id"]
                        and row["hypothesis_version"] == selected["version"]]
        if len(observations) != 1:
            raise ProtocolError("选中候选缺少唯一真实开发观察")
        observation = observations[0]
        if triggered:
            expected_action = ("request_scope_boundary_confirmation"
                               if confirmation_kind == "scope_boundary" else "request_confirmation")
            if decision["action"] != expected_action or observation["observation_id"] not in decision["observation_refs"]:
                raise ProtocolError("确认决策未引用选中候选的真实开发观察")
        run = controller.store.get("run", observation["run_id"])
        design = controller.store.get("design", selected["design_id"])
        if (not run or run["status"] != "succeeded" or run["design_id"] != selected["design_id"]
                or not design or design["status"] != "frozen"
                or design["hypothesis_version"] != selected["version"]):
            raise ProtocolError("选中开发观察未绑定成功 run 和冻结 design")
        revisions = []
        for path in controller.revisions.root.glob("REV-*/revision.json"):
            row = controller.revisions.verify(path.parent.name)
            if "RUN-" + digest({"revision_id": row["revision_id"], "split": "dev"})[:24] == observation["run_id"]:
                revisions.append(row)
        request = _evaluated_request(controller, observation, state)
        if len(revisions) == 1:
            implementation_mode = "generated_revision"
            revision = revisions[0]
            if (revision["identity"]["hypothesis_id"] != selected["hypothesis_id"]
                    or revision["identity"]["design_id"] != selected["design_id"]
                    or revision["identity"]["source_input_hashes"] != state["input_hashes"]):
                raise ProtocolError("生成 revision 的原始研究身份不一致")
            _execution_evidence(controller, revision, observation, request, state)
        elif not revisions:
            implementation_mode = "registered_implementation"
            revision = None
            _registered_execution_evidence(controller, observation, request, state)
        else:
            raise ProtocolError("确认包候选实现身份不唯一")
        control_id = source["control_hypothesis_id"]
        control_observations = [row for row in status["observations"]
                                if row["scope"] == "dev" and row["hypothesis_id"] == control_id]
        if len(control_observations) != 1:
            raise ProtocolError("确认包缺少唯一已运行的原始基线")
        baseline = control_observations[0]
        _evaluated_request(controller, baseline, state)
        effect = ((baseline["value"] - observation["value"]) if source["metric"]["direction"] == "min"
                  else (observation["value"] - baseline["value"]))
        if confirmation_kind == "positive_effect" and effect < source["min_meaningful_effect"]:
            raise ProtocolError("选中候选的实际开发效应未达到确认阈值")
        if confirmation_kind == "scope_boundary":
            if triggered:
                evidence = {row["observation_id"]: row for row in status["observations"]
                            if row["observation_id"] in decision["observation_refs"]}
            else:
                evidence = _boundary_evidence(source, status, control_id, selected["hypothesis_id"])
            expected_scopes = ["dev", *[f"dev:slice:{item['slice_id']}"
                                        for item in source.get("analysis_slices", [])]]
            expected_pairs = {(scope, hypothesis_id) for scope in expected_scopes
                              for hypothesis_id in (control_id, selected["hypothesis_id"])}
            actual_pairs = {(row["scope"], row["hypothesis_id"])
                            for row in evidence.values()}
            slice_effects = []
            for scope in expected_scopes[1:]:
                values = {row["hypothesis_id"]: row["value"] for row in evidence.values()
                          if row["scope"] == scope}
                if set(values) == {control_id, selected["hypothesis_id"]}:
                    raw = values[selected["hypothesis_id"]] - values[control_id]
                    slice_effects.append(-raw if source["metric"]["direction"] == "min" else raw)
            boundary = (abs(effect) < source["min_meaningful_effect"]
                        and len(slice_effects) >= 2
                        and max(slice_effects) >= source["min_meaningful_effect"]
                        and min(slice_effects) < source["min_meaningful_effect"])
            refs_ok = (True if not triggered
                       else set(evidence) == set(decision["observation_refs"]))
            if (not refs_ok or actual_pairs != expected_pairs or not boundary):
                raise ProtocolError("边界确认请求缺少完整的开发切片证据")
        code_files = {Path(name).as_posix(): state["input_hashes"][name] for name in state["spec"]["code_files"]}
        revision_root = (controller.revisions.path(revision["revision_id"])
                         if revision is not None else controller.project)
        candidate_identity = {
            "config": source["configs"][selected["hypothesis_id"]],
            "entrypoint": revision["entrypoint"] if revision is not None else state["spec"]["entrypoint"],
            "files": revision["files"] if revision is not None else code_files,
        }
        if implementation_mode == "registered_implementation" or contract["schema_version"] == "2.0":
            candidate_identity["implementation_mode"] = implementation_mode
        if revision is not None:
            candidate_identity.update(
                revision_id=revision["revision_id"],
                revision_manifest_sha256=file_hash(revision_root / "revision.json"),
                execution_changes=revision["identity"]["execution_changes"])
        identity = {"study_id": source["study_id"], "family_id": source["family_id"],
                    "contract_id": contract["contract_id"], "contract_sha256": digest(contract_envelope),
                    "source_manifest_sha256": file_hash(controller.manifest_path),
                    "hypothesis_id": selected["hypothesis_id"], "hypothesis_version": selected["version"],
                    "design_id": selected["design_id"], "dev_observation_id": observation["observation_id"],
                    "dev_observation_sha256": digest(observation), "train_sha256": expected["train_sha256"],
                    "dev_sha256": expected["dev_sha256"], "metric": source["metric"],
                    "evaluator_id": source["scorer_id"], "seeds": source["seeds"],
                    "min_effect": source["min_meaningful_effect"],
                    "control": {"config": source["configs"][control_id], "entrypoint": state["spec"]["entrypoint"], "files": code_files},
                    "candidate": candidate_identity}
        if contract["schema_version"] == "2.0":
            if triggered:
                refs = decision["observation_refs"]
            elif confirmation_kind == "scope_boundary":
                refs = list(evidence.keys())
            else:
                refs = [observation["observation_id"]]
            identity.update(confirmation_kind=confirmation_kind,
                            analysis_slices=source.get("analysis_slices", []),
                            dev_evidence_refs=refs)
        manifest = {"schema_version": contract["schema_version"], "kind": "confirmation_submission",
                    "submission_id": "SUB-" + digest(identity)[:24], "identity": identity}
        validate_submission(manifest, contract)
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = output.parent / (output.name + ".staging-" + uuid.uuid4().hex)
        staging.mkdir()
        write_json(staging / "bundle.json", manifest)
        write_json(staging / "contract.json", contract_envelope)
        shutil.copyfile(_file(controller.project, state["spec"]["train"]), staging / "train.json")
        candidate_root = revision_root / "code" if revision is not None else controller.project
        for role, root in (("control", controller.project), ("candidate", candidate_root)):
            for name in identity[role]["files"]:
                target = staging / role / "code" / _relative(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_file(root, name), target)
        if revision is not None:
            shutil.copyfile(_file(revision_root, "revision.json"), staging / "candidate/revision.json")
        verify_bundle(staging, pinned_public_key)
        if output.exists():
            raise ProtocolError("确认包输出目录在冻结期间已被创建，不能覆盖")
        staging.rename(output)
        staging = None
        return manifest
    except ProtocolError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ProtocolError(f"确认包导出失败: {type(error).__name__}") from None
    finally:
        if staging is not None and staging.exists():
            _plain_ancestors(staging)
            if staging.resolve().parent != output.parent.resolve() or not staging.name.startswith(output.name + ".staging-"):
                raise ProtocolError("确认包 staging 清理路径越界")
            shutil.rmtree(staging)
