"""Execute a frozen confirmation bundle without receiving held-out labels.

The supervisor verifies the service ticket, executes exact control/candidate
snapshots, then signs predictions with its registered runner key. The key is
never serialized to a job or added to a child environment. Candidate code runs
as an OS-sandbox child (Windows Low Integrity, or Linux bubblewrap when bwrap
can create unprivileged namespaces), which constrains writes and resources; the
runner's own signing-key file is additionally sealed for the execution window
(mandatory-label ``NO_READ_UP`` on Windows, mount-namespace masking inside the
sandbox on Linux) by the CLI, so that child cannot read it and forge a receipt.
This backend therefore supplies engineering evidence, not account/host isolation
or adversarial attestation: a same-account child can still read other files the
backend does not seal, and production key/label isolation still needs a separate
account or host and is outside this runner's claim.
"""
from __future__ import annotations

import importlib.metadata
import platform
import shutil
import sys
from pathlib import Path

from ..core import ProtocolError, digest, evaluator_pack, file_hash, read_json, write_json
from ..domains import ALIGNED_PREDICTION, INPUTS_SPLIT
from .confirmation_bundle import verify_bundle
from .confirmation_contracts import (public_key_b64, sign_payload, validate_ticket,
                                     verify_envelope)
from .revisions import RevisionStore
from .workers import InputArtifact, JobSpec, LocalWorker, job_invocation


BACKEND = "windows_low_integrity_engineering"
# 沙箱路径回执里可接受的执行后端名（与 popper/sandbox/__init__.py 的映射表一致）：
# 契约级的 `allowed_backend` 是运维登记的标签，这里是校验收到的 job **真的**跑在
# 某个内核隔离后端下，而不是 trusted-local。
SANDBOXED_RECEIPT_BACKENDS = ("windows_low_integrity", "linux_bubblewrap")
ROLES = ("control", "candidate")
TIMEOUT_SECONDS = 180


def _runtime_metadata(runtime_id):
    packages = {}
    for name in ("cryptography", "torch", "numpy", "scikit-learn"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    value = {"runtime_id": runtime_id, "python": sys.version, "executable": sys.executable,
             "platform": platform.platform(), "packages": packages,
             "runner_source_sha256": file_hash(Path(__file__)),
             "runtime_id_semantics": "operator_registered_engineering_label",
             "trust": "diagnostic_metadata_not_environment_or_permission_attestation"}
    return {**value, "metadata_sha256": digest(value)}


def require_aligned_prediction(evaluator_id):
    """holdout 只支持逐样本预测：其它形状必须立即失败，而不是按逐样本契约猜参数。"""
    pack = evaluator_pack(evaluator_id)
    if pack.task_shape != ALIGNED_PREDICTION:
        raise ProtocolError(
            f"未支持的任务形状: {pack.task_shape!r}（holdout 只支持逐样本预测）")
    return pack


def _validate_features(rows, evaluator_id):
    """候选可见特征：直接使用领域包声明的输入契约（不再维护第二份副本）。"""
    evaluator_pack(evaluator_id).validate_rows(rows, INPUTS_SPLIT)
    return {row["id"] for row in rows}


def _validate_prediction_rows(rows, sample_ids, evaluator_id):
    """预测的「形状 + id 覆盖」由领域包判定；runner 不接触标签。"""
    evaluator_pack(evaluator_id).validate_prediction_values(rows, sample_ids, None)


def _validate_predictions(predictions, identity, sample_ids, succeeded):
    if not isinstance(predictions, dict) or set(predictions) != set(ROLES):
        raise ProtocolError("Confirmation predictions must contain control and candidate")
    seeds = identity["seeds"]
    for role in ROLES:
        points = predictions[role]
        if not isinstance(points, list) or len(points) > len(seeds):
            raise ProtocolError("Confirmation prediction seed list is invalid")
        if succeeded and len(points) != len(seeds):
            raise ProtocolError("Successful confirmation must cover every registered seed")
        for index, point in enumerate(points):
            if (not isinstance(point, dict) or set(point) != {"seed", "rows"}
                    or type(point["seed"]) is not int or point["seed"] != seeds[index]):
                raise ProtocolError("Confirmation seeds differ from their frozen execution order")
            _validate_prediction_rows(point["rows"], sample_ids, identity["evaluator_id"])


def _copy_registered_code(source, destination, files):
    destination.mkdir(parents=True)
    source = source.resolve()
    for relative, expected_hash in files.items():
        original = (source / relative).resolve()
        target = (destination / relative).resolve()
        if (not original.is_relative_to(source) or not target.is_relative_to(destination.resolve())
                or not original.is_file() or file_hash(original) != expected_hash):
            raise ProtocolError("Frozen confirmation source is missing or changed")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)


def _import_revisions(bundle, output, manifest):
    """Adapt unedited control code, and preserve the candidate revision exactly."""
    identity = manifest["identity"]
    revisions = RevisionStore(output / "revisions")
    control = identity["control"]
    control_identity = {
        "kind": "frozen_registered_control", "submission_id": manifest["submission_id"],
        "source_manifest_sha256": identity["source_manifest_sha256"],
        "entrypoint": control["entrypoint"], "files": control["files"],
    }
    control_id = "REV-" + digest(control_identity)[:24]
    control_root = revisions.root / control_id
    _copy_registered_code(bundle / "control" / "code", control_root / "code", control["files"])
    write_json(control_root / "revision.json", {
        "schema_version": "1.0", "revision_id": control_id, "identity": control_identity,
        "entrypoint": control["entrypoint"], "files": control["files"],
        "registered_code_files": sorted(control["files"]), "added_code_files": [],
        "status": "immutable",
    })
    revisions.verify(control_id)

    candidate = identity["candidate"]
    if candidate.get("implementation_mode") == "registered_implementation":
        candidate_identity = {
            "kind": "frozen_registered_candidate", "submission_id": manifest["submission_id"],
            "source_manifest_sha256": identity["source_manifest_sha256"],
            "hypothesis_id": identity["hypothesis_id"], "design_id": identity["design_id"],
            "entrypoint": candidate["entrypoint"], "files": candidate["files"],
        }
        candidate_id = "REV-" + digest(candidate_identity)[:24]
        candidate_root = revisions.root / candidate_id
        if candidate_id == control_id:
            raise ProtocolError("Registered candidate aliases the control revision")
        _copy_registered_code(bundle / "candidate" / "code", candidate_root / "code",
                              candidate["files"])
        write_json(candidate_root / "revision.json", {
            "schema_version": "1.0", "revision_id": candidate_id,
            "identity": candidate_identity, "entrypoint": candidate["entrypoint"],
            "files": candidate["files"], "registered_code_files": sorted(candidate["files"]),
            "added_code_files": [], "status": "immutable",
        })
        revisions.verify(candidate_id)
        return revisions, {"control": control_id, "candidate": candidate_id}
    candidate_id = candidate["revision_id"]
    candidate_root = (revisions.root / candidate_id).resolve()
    if not candidate_root.is_relative_to(revisions.root) or candidate_id == control_id:
        raise ProtocolError("Candidate revision identity is invalid")
    _copy_registered_code(bundle / "candidate" / "code", candidate_root / "code", candidate["files"])
    shutil.copyfile(bundle / "candidate" / "revision.json", candidate_root / "revision.json")
    actual = revisions.verify(candidate_id)
    if (actual["files"] != candidate["files"] or actual["entrypoint"] != candidate["entrypoint"]
            or file_hash(candidate_root / "revision.json") != candidate["revision_manifest_sha256"]
            or actual["identity"].get("execution_changes") != candidate["execution_changes"]):
        raise ProtocolError("Candidate revision differs from the frozen confirmation bundle")
    return revisions, {"control": control_id, "candidate": candidate_id}


def _receipt_identity(manifest, ticket_payload, contract):
    return {"schema_version": "1.0", "kind": "confirmation_runner_receipt",
            "ticket_id": ticket_payload["ticket_id"], "contract_id": ticket_payload["contract_id"],
            "submission_id": manifest["submission_id"], "submission_sha256": digest(manifest),
            "runtime_id": contract["runtime_id"], "backend": BACKEND,
            "code_hashes": {role: manifest["identity"][role]["files"] for role in ROLES}}


def _validate_saved_result(result, expected, runner_key, identity, sample_ids):
    if not isinstance(result, dict) or set(result) != {"runner_receipt", "predictions"}:
        raise ProtocolError("Saved confirmation result is incomplete")
    payload = verify_envelope(result["runner_receipt"], runner_key)
    if set(payload) != set(expected) | {"status", "predictions_sha256", "error_type"}:
        raise ProtocolError("Saved runner receipt has unexpected fields")
    if any(payload[key] != value for key, value in expected.items()):
        raise ProtocolError("Saved runner receipt belongs to a different confirmation")
    if (payload["status"] not in {"succeeded", "failed"}
            or (payload["status"] == "succeeded" and payload["error_type"] is not None)
            or (payload["status"] == "failed" and
                (not isinstance(payload["error_type"], str) or not payload["error_type"]))):
        raise ProtocolError("Saved runner receipt status is malformed")
    if payload["predictions_sha256"] != digest(result["predictions"]):
        raise ProtocolError("Saved predictions differ from the signed runner receipt")
    _validate_predictions(result["predictions"], identity, sample_ids,
                          succeeded=payload["status"] == "succeeded")
    return result


def run_confirmation_bundle(bundle_dir, ticket, features, service_public_key,
                            runner_private_key, output_dir):
    """Run once, or return a verified terminal result without executing again.

    ``features`` is the label-free row list supplied with a signed service ticket.
    ``runner_private_key`` remains in the supervisor. A preflight failure raises
    ProtocolError; an execution failure returns a signed failed receipt. An
    interrupted output directory cannot be resumed or silently re-executed.
    """
    # Preserve the user's path until verify_bundle has rejected symlink/reparse
    # ancestors; resolving first would erase that part of the validation evidence.
    bundle, output = Path(bundle_dir).absolute(), Path(output_dir).resolve()
    if bundle.is_relative_to(output) or output.is_relative_to(bundle):
        raise ProtocolError("Confirmation output must be separate from its frozen bundle")
    manifest, contract = verify_bundle(bundle, service_public_key)
    bundle = bundle.resolve()
    contract_envelope = read_json(bundle / "contract.json")
    ticket_payload = validate_ticket(ticket, contract_envelope, manifest, service_public_key)
    runner_key = public_key_b64(runner_private_key)
    if runner_key != contract["runner_public_key"]:
        raise ProtocolError("Runner signing key does not match the registered contract")
    if contract["allowed_backend"] != BACKEND:
        raise ProtocolError("This runner cannot supply the registered execution backend")
    identity = manifest["identity"]
    # 最早能拿到 evaluator_id 的位置：在任何目录落盘、建 job 与执行之前先定形状，
    # 非逐样本形状在这里失败，不产生 job 或部分状态。
    pack = require_aligned_prediction(identity["evaluator_id"])
    sample_ids = _validate_features(features, identity["evaluator_id"])
    if digest(features) != ticket_payload["features_sha256"]:
        raise ProtocolError("Confirmation features differ from the signed service ticket")
    expected = _receipt_identity(manifest, ticket_payload, contract)
    invocation = {"submission_sha256": digest(manifest), "ticket_sha256": digest(ticket),
                  "features_sha256": digest(features), "runner_public_key": runner_key}
    invocation_path, result_path = output / "invocation.json", output / "result.json"
    if output.exists():
        if (not invocation_path.is_file() or read_json(invocation_path) != invocation
                or not result_path.is_file()):
            raise ProtocolError("Confirmation directory conflicts or was interrupted; execution cannot repeat")
        return _validate_saved_result(read_json(result_path), expected, runner_key, identity, sample_ids)

    # Exclusive directory creation consumes this local invocation before copying
    # code or launching any work. Crashes preserve it as a non-retryable attempt.
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ProtocolError("Another runner consumed this output directory; execution cannot repeat") from None
    write_json(invocation_path, invocation)
    predictions = {role: [] for role in ROLES}
    error_type = None
    try:
        write_json(output / "runtime.json", _runtime_metadata(contract["runtime_id"]))
        revisions, revision_ids = _import_revisions(bundle, output, manifest)
        inputs = output / "inputs"
        inputs.mkdir()
        feature_path = inputs / "inputs.json"
        write_json(feature_path, features)
        train_path = bundle / "train.json"
        worker = LocalWorker(output / "jobs", revisions)
        for role in ROLES:
            registered = identity[role]
            config_path = inputs / f"{role}-config.json"
            write_json(config_path, registered["config"])
            revision_id = revision_ids[role]
            revision_hash = file_hash(revisions.path(revision_id) / "revision.json")
            # 输入落位名沿用字面量：此文件受 require_aligned_prediction 守卫，故输入
            # 落位名与 aligned 声明逐字段相同（声明驱动的唯一入口是 job_invocation）。
            artifacts = (
                InputArtifact(str(train_path), "train.json", identity["train_sha256"], "training_data"),
                InputArtifact(str(feature_path), "inputs.json", file_hash(feature_path),
                              "heldout_features_without_labels"),
                InputArtifact(str(config_path), "config.json", file_hash(config_path), "frozen_intervention"),
            )
            for seed in identity["seeds"]:
                args, outputs = job_invocation(pack, seed)
                prediction_output = outputs[0]
                spec = JobSpec(
                    idempotency_key=f"confirmation:{ticket_payload['ticket_id']}:{role}:seed:{seed}",
                    revision_id=revision_id, revision_manifest_sha256=revision_hash,
                    design_id=identity["design_id"], entrypoint=registered["entrypoint"],
                    args=args, inputs=artifacts,
                    outputs=outputs, timeout_seconds=TIMEOUT_SECONDS,
                    require_edit_coverage=(role == "candidate" and
                                           registered.get("implementation_mode")
                                           != "registered_implementation"))
                worker.run(spec)
                receipt = worker.collect(spec.job_id)
                if (receipt["status"] != "succeeded"
                        or receipt["execution_backend"] not in SANDBOXED_RECEIPT_BACKENDS
                        or (role == "candidate"
                            and registered.get("implementation_mode")
                            != "registered_implementation" and
                            not receipt.get("execution_gate", {}).get("passed"))):
                    raise ProtocolError("Confirmation worker failed or did not execute the frozen revision")
                prediction_path = worker.root / spec.job_id / "workspace" / prediction_output
                rows = read_json(prediction_path)
                _validate_prediction_rows(rows, sample_ids, identity["evaluator_id"])
                predictions[role].append({"seed": seed, "rows": rows})
        _validate_predictions(predictions, identity, sample_ids, succeeded=True)
        # Recheck frozen files and every signed input/output binding after workers
        # stop, before this supervisor signs any claim about their execution.
        after_manifest, after_contract = verify_bundle(bundle, service_public_key)
        if after_manifest != manifest or after_contract != contract:
            raise ProtocolError("Confirmation bundle changed during execution")
        for revision_id in revision_ids.values():
            revisions.verify(revision_id)
        for path in worker.root.glob("JOB-*/job.json"):
            worker.collect(path.parent.name)
    except Exception as error:
        error_type = type(error).__name__

    receipt_payload = {**expected, "status": "failed" if error_type else "succeeded",
                       "predictions_sha256": digest(predictions), "error_type": error_type}
    result = {"runner_receipt": sign_payload(receipt_payload, runner_private_key),
              "predictions": predictions}
    _validate_saved_result(result, expected, runner_key, identity, sample_ids)
    write_json(result_path, result)
    return result
