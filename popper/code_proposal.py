"""Review-first code proposals grounded in a pinned AI Research skill."""
import difflib
import json
from datetime import datetime, timezone
from pathlib import Path

from .core import Experiment, ProtocolError, digest, file_hash, read_json


def _candidate(idea_dir):
    paths = [idea_dir / "phase3_revise" / "final_candidate.json",
             idea_dir / "phase2_coherence" / "refined_candidate.json",
             idea_dir / "phase2_generate" / "phase2_generate_output.json"]
    path = next((p.resolve() for p in paths if p.is_file()), None)
    if path is None or not path.is_relative_to(idea_dir):
        raise ProtocolError("Idea 运行目录中没有可用 candidate")
    value = read_json(path)
    if isinstance(value.get("final_candidate"), dict):
        value = value["final_candidate"]
    return path, value


def propose_code(idea_dir, scoop_dir, proposal_dir, experiment_dir, skill_path,
                 model, llm):
    candidate_path, candidate = _candidate(idea_dir)
    report_path = scoop_dir / "step7.json"
    if not report_path.is_file():
        raise ProtocolError("code-propose 需要 Scoop Check 报告")
    report = read_json(report_path)
    if report.get("status") != "completed":
        raise ProtocolError("code-propose 只接受 completed Scoop Check")
    if report.get("candidate_sha256") != file_hash(candidate_path):
        raise ProtocolError("Scoop Check 与 Idea candidate 不一致")
    experiment = Experiment(experiment_dir)
    try:
        state = experiment.state()
        if state["phase"] != "searching":
            raise ProtocolError("代码提案要求 Popper 实验处于 searching 阶段")
        files = []
        total = 0
        for relative in state["spec"]["code_files"]:
            path = (experiment_dir / relative).resolve()
            content = path.read_text(encoding="utf-8")
            total += len(content.encode())
            files.append({"path": relative, "sha256": file_hash(path), "content": content})
        if total > 300_000:
            raise ProtocolError("登记代码超过 code-propose 300KB 上限")
        skill = skill_path.read_text(encoding="utf-8")[:40_000]
        request = {"candidate": candidate, "scoop": {key: report.get(key) for key in
                   ("level", "label", "delta", "closest_paper_id")},
                   "objective": state["spec"]["objective"], "metric": state["spec"]["metric"],
                   "registered_configs": [state["spec"]["baseline"], *state["spec"]["candidates"]],
                   "files": files, "implementation_guidance": skill}
        request_sha = digest({"candidate_sha256": file_hash(candidate_path),
                              "scoop_sha256": file_hash(report_path),
                              "experiment_inputs": state["input_hashes"],
                              "skill_sha256": file_hash(skill_path), "model": model})
        proposal_path = proposal_dir / "proposal.json"
        if proposal_path.is_file():
            cached = read_json(proposal_path)
            if cached.get("request_sha256") != request_sha:
                raise ProtocolError("proposal 目录已绑定其他输入")
            return {"adapter": "code-proposal-v1", "status": "review_required",
                    "proposal": str(proposal_path), "diff": str(proposal_dir / "proposal.diff"),
                    "cache": "hit"}
        response = llm(
            "Propose the smallest code change that tests the supplied research hypothesis. Return JSON with summary, hypothesis, and edits. edits is a non-empty array of objects with exactly path, original_sha256, replacement. Paths must be selected from supplied files. Return complete replacement file contents, preserve the runner CLI contract, and do not edit data, protocol, metrics, or tests.", request)
        if (not isinstance(response, dict) or set(response) != {"summary", "hypothesis", "edits"}
                or not isinstance(response["summary"], str) or not response["summary"].strip()
                or not isinstance(response["hypothesis"], str) or not response["hypothesis"].strip()
                or not isinstance(response["edits"], list) or not response["edits"]):
            raise ProtocolError("code-propose 模型响应不符合契约")
        allowed = {item["path"]: item for item in files}
        seen = set(); diff_lines = []
        for edit in response["edits"]:
            if not isinstance(edit, dict) or set(edit) != {"path", "original_sha256", "replacement"}:
                raise ProtocolError("code-propose edit 字段不符合契约")
            relative = edit["path"]
            if relative not in allowed or relative in seen:
                raise ProtocolError("code-propose 只能修改无重复的已登记 code_files")
            seen.add(relative)
            original = allowed[relative]
            replacement = edit["replacement"]
            if edit["original_sha256"] != original["sha256"]:
                raise ProtocolError("code-propose 原文件 SHA-256 不匹配")
            if not isinstance(replacement, str) or not replacement.strip() or len(replacement.encode()) > 300_000:
                raise ProtocolError("code-propose replacement 不合法或过大")
            try:
                compile(replacement, relative, "exec")
            except SyntaxError as error:
                raise ProtocolError(f"code-propose Python 语法错误: {relative}:{error.lineno}") from None
            diff_lines.extend(difflib.unified_diff(
                original["content"].splitlines(True), replacement.splitlines(True),
                fromfile=f"a/{relative}", tofile=f"b/{relative}"))
        if not diff_lines:
            raise ProtocolError("code-propose 没有产生实际 diff")
        proposal = {"schema_version": "1.0", "adapter": "code-proposal-v1",
                    "status": "review_required", "created_at": datetime.now(timezone.utc).isoformat(),
                    "model": model, "request_sha256": request_sha,
                    "candidate_sha256": file_hash(candidate_path),
                    "scoop_report_sha256": file_hash(report_path),
                    "skill_sha256": file_hash(skill_path), **response}
        proposal_dir.mkdir(parents=True, exist_ok=True)
        proposal_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (proposal_dir / "proposal.diff").write_text("".join(diff_lines), encoding="utf-8")
        return {"adapter": "code-proposal-v1", "status": "review_required",
                "proposal": str(proposal_path), "diff": str(proposal_dir / "proposal.diff"),
                "cache": "miss"}
    finally:
        experiment.close()
