"""Materialize an approved code proposal as an isolated Popper experiment."""
import json
import shutil
import difflib
from pathlib import Path

from .core import Experiment, ProtocolError, file_hash, initialize, read_json


ROUTER = '''"""Generated Popper code-variant router."""
import argparse, json, subprocess, sys, tempfile
from pathlib import Path

parser = argparse.ArgumentParser()
for name in ("train", "input", "output", "config"):
    parser.add_argument("--" + name, required=True)
parser.add_argument("--seed", required=True)
args = parser.parse_args()
config = json.loads(Path(args.config).read_text(encoding="utf-8"))
variant = config.pop("__popper_variant", None)
if variant not in {"baseline", "candidate"}:
    raise SystemExit("invalid __popper_variant")
root = Path(__file__).resolve().parent / "variants" / variant
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
    json.dump(config, handle)
    clean_config = handle.name
try:
    command = [sys.executable, str(root / ENTRYPOINT), "--train", args.train,
               "--input", args.input, "--output", args.output,
               "--config", clean_config, "--seed", args.seed]
    raise SystemExit(subprocess.run(command, cwd=root, check=False).returncode)
finally:
    Path(clean_config).unlink(missing_ok=True)
'''


def materialize(proposal_dir, experiment_dir, output_dir, config_index, approved):
    if not approved:
        raise ProtocolError("code-materialize 需要显式 --approved")
    proposal_path = proposal_dir / "proposal.json"
    if not proposal_path.is_file():
        raise ProtocolError("缺少 code proposal")
    proposal = read_json(proposal_path)
    if proposal.get("status") != "review_required" or proposal.get("adapter") != "code-proposal-v1":
        raise ProtocolError("code proposal 状态或契约不合法")
    if output_dir.exists():
        manifest = output_dir / "lineage.json"
        if manifest.is_file() and read_json(manifest).get("proposal_sha256") == file_hash(proposal_path):
            Experiment(output_dir).close()
            return {"adapter": "code-variant-v1", "status": "ready", "cache": "hit",
                    "project": str(output_dir), "lineage": str(manifest)}
        raise ProtocolError("code variant 输出目录已存在且不匹配")
    source = Experiment(experiment_dir)
    try:
        state = source.state()
        if state["phase"] != "searching":
            raise ProtocolError("源实验必须处于 searching 阶段")
        spec = state["spec"]
        choices = [spec["baseline"], *spec["candidates"]]
        if not 0 <= config_index < len(choices):
            raise ProtocolError("config-index 越界；0 是 baseline，后续是 candidates")
        selected = choices[config_index]
        if "__popper_variant" in selected:
            raise ProtocolError("配置使用了保留字段 __popper_variant")
        if not isinstance(proposal.get("edits"), list) or not proposal["edits"]:
            raise ProtocolError("proposal edits 不合法")
        edits = {}
        expected_diff = []
        for edit in proposal["edits"]:
            if not isinstance(edit, dict) or set(edit) != {"path", "original_sha256", "replacement"}:
                raise ProtocolError("proposal edit 字段不合法")
            relative = edit["path"]
            if relative in edits:
                raise ProtocolError("proposal 包含重复代码文件")
            edits[relative] = edit
            if relative not in spec["code_files"]:
                raise ProtocolError("proposal 包含未登记代码文件")
            source_path = (experiment_dir / relative).resolve()
            if file_hash(source_path) != edit["original_sha256"]:
                raise ProtocolError("源代码已变化，拒绝物化旧 proposal")
            try:
                compile(edit["replacement"], relative, "exec")
            except (SyntaxError, TypeError):
                raise ProtocolError("proposal replacement 不再通过 Python 语法检查") from None
            original = source_path.read_text(encoding="utf-8")
            expected_diff.extend(difflib.unified_diff(
                original.splitlines(True), edit["replacement"].splitlines(True),
                fromfile=f"a/{relative}", tofile=f"b/{relative}"))
        diff_path = proposal_dir / "proposal.diff"
        if not diff_path.is_file() or diff_path.read_text(encoding="utf-8") != "".join(expected_diff):
            raise ProtocolError("proposal.diff 与结构化 edits 不一致")
        output_dir.mkdir(parents=True)
        for relative in [spec["train"], spec["dev"], spec["test"], *spec.get("provenance_files", [])]:
            destination = output_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(experiment_dir / relative, destination)
        variant_files = []
        for relative in spec["code_files"]:
            original = (experiment_dir / relative).read_text(encoding="utf-8")
            replacement = edits.get(relative, {}).get("replacement", original)
            for variant, content in (("baseline", original), ("candidate", replacement)):
                target = output_dir / "variants" / variant / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                variant_files.append(str(target.relative_to(output_dir)).replace("\\", "/"))
        router = ROUTER.replace("ENTRYPOINT", repr(spec["entrypoint"]))
        (output_dir / "variant_router.py").write_text(router, encoding="utf-8")
        baseline = {**selected, "__popper_variant": "baseline"}
        candidate = {**selected, "__popper_variant": "candidate"}
        derived = {"name": spec["name"] + " · code variant", "objective": proposal["hypothesis"],
                   "entrypoint": "variant_router.py", "code_files": ["variant_router.py", *variant_files],
                   "provenance_files": [*spec.get("provenance_files", []), "lineage.json"],
                   "train": spec["train"], "dev": spec["dev"], "test": spec["test"],
                   "baseline": baseline, "candidates": [candidate], "metric": spec["metric"],
                   "seeds": spec["seeds"], "budget": 1,
                   "timeout_seconds": spec["timeout_seconds"],
                   "min_improvement": spec["min_improvement"]}
        lineage = {"schema_version": "1.0", "adapter": "code-variant-v1",
                   "proposal_sha256": file_hash(proposal_path), "source_project": str(experiment_dir),
                   "source_input_hashes": state["input_hashes"], "selected_config_index": config_index,
                   "selected_config": selected, "review": "approved_by_cli_flag"}
        (output_dir / "lineage.json").write_text(json.dumps(lineage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output_dir / "experiment.json").write_text(json.dumps(derived, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        initialized = initialize(output_dir)
        return {"adapter": "code-variant-v1", "status": "ready", "cache": "miss",
                "project": str(output_dir), "lineage": str(output_dir / "lineage.json"),
                "popper": initialized}
    except Exception:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        raise
    finally:
        source.close()
