"""Reproducible, bounded CUDA training acceptance fixture; no model/API calls.

Run with a CUDA-enabled PyTorch interpreter and an explicit device assignment:
    $env:POPPER_GPU_DEVICES = '0'
    .venv-gpu/Scripts/python evaluation/run_gpu_training_trial.py

This exercises immutable revisions, the real LocalWorker sandbox, changed-line
coverage, CUDA forward/backward/optimizer work, and independent dev scoring.
The baseline and candidate are hand-authored engineering fixtures. Their width
change is not a model-generated discovery or evidence of scientific advantage.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from popper.core import (Experiment, ProtocolError, file_hash, initialize,
                         model_inputs, read_json, write_json)
from popper.research.evaluation_service import IndependentEvaluator
from popper.research.execution import EXECUTED_COVERAGE
from popper.research.revisions import CodeEdit, RevisionStore
from popper.research.workers import InputArtifact, JobSpec, LocalWorker


SEEDS = (11, 29, 47)
TRAINING_STEPS = 60
TIMEOUT_SECONDS = 180
HYPOTHESIS_ID = "H-GPU-ENGINEERING-FIXTURE"
DESIGN_ID = "D-GPU-MLP-30-32-2-60-STEPS"

# One actual executable change (16 -> 32) is frozen in the candidate revision.
# All diagnostics and outputs below are identical between fixture versions.
TRAINING_SOURCE = r'''"""Fixed CUDA MLP engineering fixture, not autonomous research."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

# Required for deterministic CUDA matrix multiplication; set before CUDA init.
os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"

import torch

HIDDEN_WIDTH = 16


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main():
    parser = argparse.ArgumentParser()
    for name in ("train", "input", "output", "config", "seed", "diagnostics", "tensors"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    config = read_json(args.config)
    if config != {"model": "mlp_cuda_fixture", "hidden_width": HIDDEN_WIDTH, "steps": 60}:
        raise ValueError("Frozen fixture configuration does not match this code version")
    seed = int(args.seed)
    torch.set_num_threads(1)
    torch.manual_seed(seed)
    if not torch.cuda.is_available():
        raise RuntimeError("Real CUDA is required; CPU fallback is forbidden")
    device = torch.device("cuda:0")
    torch.cuda.set_device(device)
    torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    train = read_json(args.train)
    inputs = read_json(args.input)
    if any(set(row) != {"id", "features"} for row in inputs):
        raise ValueError("Development input must contain features and IDs only")
    if any(len(row["features"]) != 30 for row in train + inputs):
        raise ValueError("This fixed MLP expects exactly 30 features")

    torch.cuda.synchronize(device)
    torch.cuda.reset_peak_memory_stats(device)
    started = time.perf_counter()
    x = torch.tensor([row["features"] for row in train], dtype=torch.float32, device=device)
    y = torch.tensor([row["label"] for row in train], dtype=torch.long, device=device)
    dev_x = torch.tensor([row["features"] for row in inputs], dtype=torch.float32, device=device)
    # Fit preprocessing only on training rows, and perform it on the assigned GPU.
    mean = x.mean(dim=0)
    scale = x.std(dim=0, unbiased=False).clamp_min(1e-6)
    x = (x - mean) / scale
    dev_x = (dev_x - mean) / scale
    model = torch.nn.Sequential(torch.nn.Linear(30, HIDDEN_WIDTH), torch.nn.ReLU(),
                                torch.nn.Linear(HIDDEN_WIDTH, 2)).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01, foreach=False)
    criterion = torch.nn.CrossEntropyLoss()
    parameter = next(model.parameters())
    parameter_before = parameter.detach().clone()
    first_loss = None
    losses = []
    event_start = torch.cuda.Event(enable_timing=True)
    event_end = torch.cuda.Event(enable_timing=True)
    torch.cuda.synchronize(device)
    training_started = time.perf_counter()
    event_start.record()
    model.train()
    for step in range(config["steps"]):
        optimizer.zero_grad(set_to_none=True)
        logits = model(x)
        loss = criterion(logits, y)
        if not bool(torch.isfinite(loss).item()):
            raise RuntimeError("Training loss became non-finite")
        if first_loss is None:
            first_loss = float(loss.detach().item())
        loss.backward()
        if parameter.grad is None or parameter.grad.device != device:
            raise RuntimeError("The model did not produce CUDA gradients")
        optimizer.step()
        losses.append(float(loss.detach().item()))
    event_end.record()
    torch.cuda.synchronize(device)
    training_wall_seconds = time.perf_counter() - training_started
    gradient = parameter.grad.detach().clone()
    parameter_after = parameter.detach().clone()
    gradient_l2 = float(torch.linalg.vector_norm(gradient).item())
    update_l2 = float(torch.linalg.vector_norm(parameter_after - parameter_before).item())
    if not bool(torch.isfinite(gradient).all().item()) or gradient_l2 <= 0 or update_l2 <= 0:
        raise RuntimeError("Expected finite nonzero gradients and an actual parameter update")
    model.eval()
    with torch.no_grad():
        final_loss = float(criterion(model(x), y).item())
        dev_logits = model(dev_x)
        predictions = dev_logits.argmax(dim=1)
    torch.cuda.synchronize(device)
    compute_wall_seconds = time.perf_counter() - started
    if any(tensor.device != device for tensor in (x, y, dev_x, parameter, gradient,
                                                  dev_logits, predictions)):
        raise RuntimeError("Training or prediction silently left the assigned CUDA device")

    # Preserve actual CUDA storage tags, tensors, gradients and updated parameters.
    # The controlling process loads this fixed fixture artifact with weights_only
    # and checks storage locations before mapping tensors to CPU for validation.
    torch.save({"training_tensor": x[:8].detach().clone(),
                "development_logits": dev_logits.detach().clone(),
                "gradient": gradient, "parameter_before": parameter_before,
                "parameter_after": parameter_after,
                "model_state": {name: value.detach().clone()
                                for name, value in model.state_dict().items()}}, args.tensors)
    result = [{"id": row["id"], "prediction": int(prediction)}
              for row, prediction in zip(inputs, predictions.cpu().tolist())]
    Path(args.output).write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
    properties = torch.cuda.get_device_properties(device)
    diagnostics = {
        "schema_version": "1.0", "classification": "fixed_cuda_training_fixture",
        "seed": seed, "steps": config["steps"], "architecture": [30, HIDDEN_WIDTH, 2],
        "torch_version": str(torch.__version__), "torch_cuda_version": torch.version.cuda,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "device": str(device), "device_name": properties.name,
        "device_total_memory_bytes": properties.total_memory,
        "training_tensor_device": str(x.device), "label_tensor_device": str(y.device),
        "parameter_device": str(parameter.device), "gradient_device": str(gradient.device),
        "logits_device": str(dev_logits.device), "prediction_device": str(predictions.device),
        "gradient_l2": gradient_l2, "parameter_update_l2": update_l2,
        "first_training_loss": first_loss, "final_training_loss": final_loss,
        "training_losses": losses, "train_rows": len(train), "dev_rows": len(inputs),
        "peak_allocated_memory_bytes": torch.cuda.max_memory_allocated(device),
        "peak_reserved_memory_bytes": torch.cuda.max_memory_reserved(device),
        "allocated_memory_bytes": torch.cuda.memory_allocated(device),
        "training_cuda_elapsed_ms": event_start.elapsed_time(event_end),
        "training_wall_seconds": training_wall_seconds,
        "compute_wall_seconds": compute_wall_seconds,
        "total_wall_seconds": time.perf_counter() - started,
        "preprocessing_fit_split": "train", "test_used": False,
        "diagnostic_trust": "fixed_fixture_self_report_plus_tensor_artifact_validation",
    }
    Path(args.diagnostics).write_text(json.dumps(diagnostics, indent=2, allow_nan=False),
                                      encoding="utf-8")
    print(json.dumps({"seed": seed, "device": str(device), "steps": config["steps"],
                      "final_training_loss": final_loss}), flush=True)


if __name__ == "__main__":
    main()
'''


def validate_cuda_evidence(diagnostics, tensor_path, prediction_path, seed, dev_count):
    """Validate fixed-fixture artifacts without trusting a claimed accuracy."""
    import torch

    devices = []

    def map_storage(storage, location):
        devices.append(location)
        return storage

    evidence = torch.load(tensor_path, map_location=map_storage, weights_only=True)
    if not devices or set(devices) != {"cuda:0"}:
        raise ProtocolError("Tensor artifact does not contain exclusively real CUDA storage tags")
    for key in ("training_tensor", "development_logits", "gradient",
                "parameter_before", "parameter_after"):
        if not isinstance(evidence.get(key), torch.Tensor) or not bool(torch.isfinite(evidence[key]).all()):
            raise ProtocolError(f"Missing or non-finite tensor evidence: {key}")
    gradient, before, after = (evidence[name] for name in
                                ("gradient", "parameter_before", "parameter_after"))
    if any(list(tensor.shape) != [32, 30] for tensor in (gradient, before, after)):
        raise ProtocolError("Saved parameters do not implement the frozen width-32 revision")
    gradient_l2 = float(torch.linalg.vector_norm(gradient).item())
    update_l2 = float(torch.linalg.vector_norm(after - before).item())
    if gradient_l2 <= 0 or update_l2 <= 0:
        raise ProtocolError("Saved gradient or parameter update is zero")
    logits = evidence["development_logits"]
    predictions = read_json(prediction_path)
    if list(logits.shape) != [dev_count, 2] or len(predictions) != dev_count:
        raise ProtocolError("Saved CUDA logits do not cover the development split")
    if logits.argmax(dim=1).tolist() != [row["prediction"] for row in predictions]:
        raise ProtocolError("Prediction artifact disagrees with the saved CUDA logits")
    if (diagnostics.get("seed") != seed or diagnostics.get("steps") != TRAINING_STEPS
            or diagnostics.get("architecture") != [30, 32, 2]
            or diagnostics.get("dev_rows") != dev_count or diagnostics.get("test_used") is not False):
        raise ProtocolError("CUDA diagnostics disagree with the frozen job contract")
    for field in ("device", "training_tensor_device", "label_tensor_device", "parameter_device",
                  "gradient_device", "logits_device", "prediction_device"):
        if diagnostics.get(field) != "cuda:0":
            raise ProtocolError(f"CUDA device mismatch: {field}")
    for field in ("peak_allocated_memory_bytes", "peak_reserved_memory_bytes",
                  "training_cuda_elapsed_ms", "training_wall_seconds", "compute_wall_seconds",
                  "total_wall_seconds", "gradient_l2", "parameter_update_l2"):
        value = diagnostics.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ProtocolError(f"CUDA diagnostic must be finite and positive: {field}")
    if not math.isclose(gradient_l2, diagnostics["gradient_l2"], rel_tol=1e-4, abs_tol=1e-8):
        raise ProtocolError("Saved gradient disagrees with CUDA diagnostic")
    if not math.isclose(update_l2, diagnostics["parameter_update_l2"], rel_tol=1e-4, abs_tol=1e-8):
        raise ProtocolError("Saved parameter update disagrees with CUDA diagnostic")
    return {"passed": True, "storage_devices": sorted(set(devices)),
            "gradient_l2_from_saved_tensor": gradient_l2,
            "parameter_update_l2_from_saved_tensor": update_l2,
            "predictions_match_saved_cuda_logits": True,
            "trust": "fixed_fixture_artifacts_not_adversarial_attestation"}


def run_trial(trial):
    project = trial / "project"
    source = ROOT / "examples" / "breast-cancer-wisconsin"
    project.mkdir(parents=True)
    # Initialization validates and hashes the public held-out file. No test
    # predictions are requested, and test data is never copied into GPU jobs.
    for name in ("train.json", "dev.json", "test.json", "dataset_source.json"):
        shutil.copyfile(source / name, project / name)
    baseline = {"model": "mlp_cuda_fixture", "hidden_width": 16, "steps": TRAINING_STEPS}
    candidate = {**baseline, "hidden_width": 32}
    write_json(project / "experiment.json", {
        "name": "CUDA training engineering acceptance fixture",
        "objective": "Verify real GPU training and independent dev scoring; no scientific comparison",
        "entrypoint": "model.py", "code_files": ["model.py"],
        "provenance_files": ["dataset_source.json"],
        "train": "train.json", "dev": "dev.json", "test": "test.json",
        "baseline": baseline, "candidates": [candidate],
        "metric": {"name": "accuracy", "direction": "max"}, "seeds": list(SEEDS),
        "budget": 1, "timeout_seconds": TIMEOUT_SECONDS, "min_improvement": 0.02,
    })
    model_path = project / "model.py"
    model_path.write_text(TRAINING_SOURCE, encoding="utf-8")
    state = initialize(project)
    revisions = RevisionStore(trial / "revisions")
    revision = revisions.create(
        project, HYPOTHESIS_ID, DESIGN_ID,
        [CodeEdit("model.py", TRAINING_SOURCE.replace("HIDDEN_WIDTH = 16", "HIDDEN_WIDTH = 32", 1),
                  file_hash(model_path))], actor="hand_authored_gpu_engineering_fixture")
    revision_id = revision["revision_id"]
    revision_sha = file_hash(revisions.path(revision_id) / "revision.json")
    input_dir = trial / "input-artifacts"
    input_dir.mkdir()
    dev_rows = read_json(project / "dev.json")
    dev_inputs, config_path = input_dir / "inputs.json", input_dir / "config.json"
    write_json(dev_inputs, model_inputs(dev_rows, state["evaluator_id"]))
    write_json(config_path, candidate)
    train_path = project / "train.json"
    artifacts = (
        InputArtifact(str(train_path), "train.json", file_hash(train_path), "training_data"),
        InputArtifact(str(dev_inputs), "inputs.json", file_hash(dev_inputs),
                      "development_features_without_labels"),
        InputArtifact(str(config_path), "config.json", file_hash(config_path), "frozen_intervention"),
    )
    worker = LocalWorker(trial / "jobs", revisions)
    summary = {
        "schema_version": "1.0", "classification": "fixed_cuda_training_engineering_trial",
        "passed": False, "passed_scope": "cuda_training_revision_execution_and_independent_dev_scoring",
        "claim_limit": "Hand-authored width-16/32 fixtures; only width-32 runs. This does not establish "
                       "autonomous research, method novelty, or a scientific advantage over a baseline.",
        "autonomous_implementation_verified": False, "scientific_advantage_verified": False,
        "baseline_executed": False, "candidate": candidate,
        "revision_id": revision_id, "revision_manifest_sha256": revision_sha,
        "python_executable": sys.executable, "seeds": list(SEEDS),
        "limits": {"gpu_count": 1, "jobs": 3, "steps_per_seed": TRAINING_STEPS,
                   "timeout_seconds_per_seed": TIMEOUT_SECONDS, "api_calls": 0, "network_downloads": 0},
        "test_exposure": {"initialized_and_hash_verified": True,
                          "provided_to_worker": False, "scored": False,
                          "security_isolated_from_same_account": False},
        "worker_receipts": [], "cuda_diagnostics": [], "tensor_evidence_validation": [],
        "independent_evaluation": None, "error_type": None,
    }
    write_json(trial / "trial-summary.json", summary)
    predictions = []
    try:
        for seed in SEEDS:
            output = f"outputs/predictions-{seed}.json"
            diagnostic_output = f"outputs/cuda-diagnostics-{seed}.json"
            tensor_output = f"outputs/cuda-tensors-{seed}.pt"
            spec = JobSpec(
                idempotency_key=f"gpu-training-fixture:{revision_id}:seed:{seed}",
                revision_id=revision_id, revision_manifest_sha256=revision_sha,
                design_id=DESIGN_ID, entrypoint="model.py",
                args=("--train", "inputs/train.json", "--input", "inputs/inputs.json",
                      "--output", output, "--config", "inputs/config.json", "--seed", str(seed),
                      "--diagnostics", diagnostic_output, "--tensors", tensor_output),
                inputs=artifacts, outputs=(output, diagnostic_output, tensor_output),
                timeout_seconds=TIMEOUT_SECONDS, gpu_count=1, require_edit_coverage=True)
            print(f"CUDA training fixture: seed {seed}, 60 steps, immutable revision {revision_id}", flush=True)
            receipt = worker.run(spec)
            # Verify each manifest and declared artifact through the worker path.
            receipt = worker.collect(spec.job_id)
            summary["worker_receipts"].append(receipt)
            if receipt["status"] != "succeeded":
                summary["failure_context"] = worker.failure_context(spec.job_id)
                raise ProtocolError(f"GPU worker failed: {receipt['status']} / {receipt.get('error_type')}")
            # This fixture changes known statements in model.py, so real coverage is
            # required: the graded gate also passes with ambiguous/partial evidence, and
            # "nothing detected to execute" must not count as executed changed code here.
            if (receipt["execution_backend"] != "windows_low_integrity"
                    or receipt.get("execution_gate", {}).get("coverage") not in EXECUTED_COVERAGE):
                raise ProtocolError("Real sandbox execution and changed-statement coverage are required")
            workspace = worker.root / spec.job_id / "workspace"
            diagnostic = read_json(workspace / diagnostic_output)
            summary["cuda_diagnostics"].append(diagnostic)
            verified = validate_cuda_evidence(diagnostic, workspace / tensor_output,
                                              workspace / output, seed, len(dev_rows))
            summary["tensor_evidence_validation"].append({"seed": seed, **verified})
            predictions.append({"seed": seed, "path": str(workspace / output),
                                "sha256": file_hash(workspace / output)})
            write_json(trial / "trial-summary.json", summary)
        evaluator = IndependentEvaluator(project, trial / "independent-evaluations")
        summary["independent_evaluation"] = evaluator.score_artifacts(
            state, "dev", "GPU-TRAINING-DEV", predictions)
        experiment = Experiment(project)
        try:
            final_state = experiment.verify_inputs()
            if final_state["phase"] != "searching":
                raise ProtocolError("Engineering trial unexpectedly advanced the core experiment phase")
        finally:
            experiment.close()
        revisions.verify(revision_id)
        for receipt in summary["worker_receipts"]:
            worker.collect(receipt["job_id"])
        summary["passed"] = True
    except Exception as error:
        summary["error_type"] = type(error).__name__
        summary["error"] = str(error)
    finally:
        write_json(trial / "trial-summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        help="New output directory; existing directories are never overwritten")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    trial = (args.output_dir or ROOT / "evaluation" / "runs" / f"gpu-training-{stamp}").resolve()
    if trial.exists():
        parser.error("Output directory already exists; choose a new directory")
    if not any(value.strip() for value in os.environ.get("POPPER_GPU_DEVICES", "").split(",")):
        parser.error("Set POPPER_GPU_DEVICES to an explicitly assigned CUDA device, e.g. 0")
    try:
        summary = run_trial(trial)
    except Exception as error:
        trial.mkdir(parents=True, exist_ok=True)
        summary = {"passed": False, "classification": "fixed_cuda_training_engineering_trial",
                   "error_type": type(error).__name__, "error": str(error), "stage": "initialization"}
        write_json(trial / "trial-summary.json", summary)
    result = {"passed": summary["passed"], "summary": str(trial / "trial-summary.json"),
              "error_type": summary.get("error_type"),
              "mean_dev_accuracy": (summary.get("independent_evaluation") or {}).get("mean")}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if summary["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
