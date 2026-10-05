"""Public application execution documentation, not scientific state or scoring."""
from pathlib import Path

from ..core import ProtocolError, file_hash, read_json
from ..identity import EntityKind, parse_id


def execution_contracts(project, research_runs, study_ids=None):
    """Pin only explicitly bound, project-owned execution interfaces.

    Never read private reference data or export the binding's protected paths.
    Unknown Worker profiles are not guessed from the question or filenames.
    """
    project = Path(project).resolve()
    contracts = []
    for run in dict.fromkeys(Path(value).resolve() for value in research_runs):
        manifest_path = run / "research.json"
        binding_path = run / "oscillator-worker-binding.json"
        if not manifest_path.is_file() or not binding_path.is_file():
            continue
        manifest = read_json(manifest_path)
        if Path(manifest["project"]).resolve() != project:
            continue
        study_id = manifest["study_id"]
        parse_id(study_id, EntityKind.STUDY)
        if study_ids is not None and study_id not in study_ids:
            continue
        binding = read_json(binding_path)
        if binding.get("kind") != "windows_metered_rhs_v1":
            continue
        if (binding.get("schema_version") != "1.0"
                or binding.get("research_sha256") != file_hash(manifest_path)
                or manifest.get("scorer_id") != "oscillator-trajectory-v1"):
            raise ProtocolError("执行接口绑定已变化，不能组装旧契约上下文")
        for name, key in (("oscillator_worker.py", "adapter_sha256"), ("oscillator_execution.py", "execution_sha256")):
            if binding.get(key) != file_hash(Path(__file__).with_name(name)):
                raise ProtocolError("Worker 执行实现与冻结绑定不一致，未调用模型")
        sources = {}
        for name in ("oscillator_driver.py", "oscillator_client.py", "oscillator_broker.py"):
            path = Path(__file__).with_name(name)
            sha = file_hash(path)
            if binding.get("implementation_files", {}).get(str(path)) != sha:
                raise ProtocolError("执行接口源码与冻结绑定不一致，未调用模型")
            sources[name] = sha
        contracts.append({
            "kind": "public_execution_contract", "schema_version": "1.0",
            "study_id": study_id, "source": "verified frozen Worker interface documentation",
            "binding_sha256": file_hash(binding_path), "interface_source_sha256": sources,
            "entrypoint": "model.py must define solve(rhs, y0, times, budget, config)",
            "inputs": {"y0": "this case's [q0, v0]", "times": "this case's prescribed sample times",
                       "budget": "accepted RHS queries allowed for this case, not the aggregate over a Job",
                       "config": "the registered candidate configuration"},
            "output": "len(times) rows [q, v], beginning at y0; None explicitly reports a failed case",
            "rhs": {"signature": "rhs(t, [q, v]) returns [dq/dt, dv/dt]",
                    "time": "finite and 0 <= t <= this case t_end + 1e-9",
                    "state": "two finite numbers, each abs <= 1e100",
                    "counting": "every accepted host call counts, including stages and implicit iterations",
                    "failure": "invalid input or budget exhaustion fails the session; finish cannot undo it"},
            "scope": "interface documentation only; no method recommendation, reference trajectories, scoring source or approval",
        })
    return contracts
