"""Pinned adapters for executable capabilities supplied by local open-source projects."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .core import Experiment, ProtocolError, digest, file_hash, read_json


DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "integrations" / "vendors.json"


class VendorRegistry:
    def __init__(self, project_root=None, registry_path=None):
        self.project_root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        self.registry_path = Path(registry_path or DEFAULT_REGISTRY).resolve()
        self.registry = read_json(self.registry_path)
        if (not isinstance(self.registry, dict) or self.registry.get("schema_version") != "1.1"
                or not isinstance(self.registry.get("components"), dict)):
            raise ProtocolError("vendors.json 不符合 1.0 契约")
        self.workspace_root = self.project_root.parent.resolve()

    def component(self, component_id):
        value = self.registry["components"].get(component_id)
        if not isinstance(value, dict):
            raise ProtocolError(f"未知开源组件: {component_id}")
        common = {"kind", "name", "source_root", "license", "license_file",
                  "skill_file", "capabilities", "sha256"}
        required = common | ({"entrypoint"} if value.get("kind") == "executable" else set())
        if (value.get("kind") not in {"executable", "protocol"} or set(value) != required
                or value["license"] not in {"MIT", "Apache-2.0"}):
            raise ProtocolError(f"开源组件登记不完整: {component_id}")
        root = (self.registry_path.parent / value["source_root"]).resolve()
        if not root.is_relative_to(self.workspace_root) or not root.is_dir():
            raise ProtocolError(f"开源组件目录缺失或越界: {component_id}")
        return value, root

    def resolve(self, component_id, relative):
        _, root = self.component(component_id)
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ProtocolError(f"开源组件文件缺失或越界: {component_id}/{relative}")
        return path

    def inspect(self):
        components = []
        for component_id in sorted(self.registry["components"]):
            value, _ = self.component(component_id)
            checked = {}
            for relative, expected in value["sha256"].items():
                actual = file_hash(self.resolve(component_id, relative))
                if actual != expected:
                    raise ProtocolError(f"开源组件指纹不匹配: {component_id}/{relative}")
                checked[relative] = actual
            if value["license_file"] not in checked or value["skill_file"] not in checked:
                raise ProtocolError(f"开源组件未锁定许可证或 SKILL: {component_id}")
            if value["kind"] == "executable" and value["entrypoint"] not in checked:
                raise ProtocolError(f"开源组件未锁定入口: {component_id}")
            components.append({"id": component_id, "name": value["name"],
                               "kind": value["kind"], "license": value["license"],
                               "capabilities": value["capabilities"],
                               "entrypoint": (str(self.resolve(component_id, value["entrypoint"]))
                                              if value["kind"] == "executable" else None),
                               "files_verified": len(checked), "status": "verified"})
        return {"status": "verified", "registry": str(self.registry_path), "components": components}

    def _run_dir(self, raw):
        path = Path(raw)
        if not path.is_absolute():
            path = self.project_root / path
        path = path.resolve()
        runs_root = (self.project_root / "integrations" / "runs").resolve()
        if not path.is_relative_to(runs_root):
            raise ProtocolError("集成运行目录必须位于 integrations/runs 内")
        return path

    def _execute(self, component_id, args, timeout=30):
        self.inspect()
        value, _ = self.component(component_id)
        if value["kind"] != "executable":
            raise ProtocolError(f"协议组件不可直接执行: {component_id}")
        entrypoint = self.resolve(component_id, value["entrypoint"])
        environment = {key: val for key, val in os.environ.items()
                       if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONHASHSEED": "0"})
        try:
            completed = subprocess.run([sys.executable, str(entrypoint), *map(str, args)],
                                       cwd=self.project_root, env=environment,
                                       stdin=subprocess.DEVNULL, capture_output=True,
                                       text=True, encoding="utf-8", errors="replace",
                                       timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise ProtocolError(f"{component_id} 调用失败: {type(error).__name__}: {error}") from error
        result = {"component": component_id, "entrypoint_sha256": file_hash(entrypoint),
                  "returncode": completed.returncode, "stdout": completed.stdout,
                  "stderr": completed.stderr}
        if completed.returncode != 0:
            raise ProtocolError(f"{component_id} 返回 {completed.returncode}: {completed.stderr.strip()}")
        return result

    def paper_search(self, run_dir, queries, start_year, end_year, max_papers=10,
                     sources=None, min_score=None, parallel=True, trusted_local=False):
        if not trusted_local:
            raise ProtocolError("当前执行非沙箱；paper-search 需要显式 --trusted-local")
        target = self._run_dir(run_dir)
        queries = [str(q).strip() for q in queries if str(q).strip()]
        allowed = {"semantic_scholar", "open_alex", "arxiv", "openreview", "crossref", "dblp"}
        sources = list(dict.fromkeys(sources or ["semantic_scholar", "open_alex", "arxiv", "crossref", "dblp"]))
        if not queries or not (1900 <= start_year <= end_year <= 2200) or not (1 <= max_papers <= 100):
            raise ProtocolError("paper-search 查询、年份或数量不合法")
        if set(sources) - allowed:
            raise ProtocolError("paper-search 包含未知来源")
        request = {"queries": queries, "start_year": start_year, "end_year": end_year,
                   "max_papers": max_papers, "sources": sources, "min_score": min_score,
                   "parallel": bool(parallel), "execution_policy": "bounded-sources-v2"}
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        output = target / "literature" / f"search-{fingerprint}.json"
        if output.is_file():
            cached = read_json(output)
            cached["cache"] = "hit"
            return cached
        self.inspect()
        value, root = self.component("researchstudio_paper_search")
        scripts = (root / value["entrypoint"]).parent
        env_names = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "SEMANTICSCHOLAR_API_KEY",
                     "OPENALEX_API_KEY", "OPENREVIEW_USER", "OPENREVIEW_PASS",
                     "PAPER_SEARCH_TIMEOUT_SECONDS", "PAPER_SEARCH_CONNECT_TIMEOUT_SECONDS",
                     "PAPER_SEARCH_MAX_ATTEMPTS", "ARXIV_MIN_INTERVAL"}
        environment = {k: v for k, v in os.environ.items() if k.upper() in env_names}
        environment.update({"PAPER_SEARCH_TIMEOUT_SECONDS": "15",
                            "PAPER_SEARCH_CONNECT_TIMEOUT_SECONDS": "8",
                            "PAPER_SEARCH_MAX_ATTEMPTS": "1"})
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONHASHSEED": "0"})
        completed = subprocess.run(
            [sys.executable, "-m", "popper.vendor_worker", "paper-search", str(scripts)],
            cwd=self.project_root, env=environment, input=json.dumps(request), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=450, check=False)
        if completed.returncode != 0:
            raise ProtocolError(f"paper-search 返回 {completed.returncode}: {completed.stderr.strip()}")
        payload = json.loads(completed.stdout)
        warnings = [line for line in completed.stderr.splitlines() if line.strip()]
        result = {"schema_version": "1.0", "adapter": "paper-search-v1", "cache": "miss",
                  "request_sha256": fingerprint, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                  "request": request, "vendor_entrypoint_sha256": file_hash(root / value["entrypoint"]),
                  "warnings": warnings,
                  "errors": [line for line in warnings if any(word in line.lower() for word in
                             ("error", "failed", "unavailable", "exited", "invalid"))], **payload}
        output.parent.mkdir(parents=True, exist_ok=True)
        result["artifact"] = str(output)
        temp = output.with_suffix(".tmp")
        temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(output)
        return result

    def scoop_run(self, idea_run_dir, scoop_run_dir, base_url, model, start_year, end_year,
                  refresh_fulltext=False, trusted_local=False):
        if not trusted_local:
            raise ProtocolError("当前执行非沙箱；scoop-run 需要显式 --trusted-local")
        from .scoop import ScoopRun, make_json_client
        idea = self._run_dir(idea_run_dir)
        target = self._run_dir(scoop_run_dir)
        self.inspect()
        run = ScoopRun(idea, target,
                       lambda queries, start, end: self.paper_search(
                           target, queries, start, end, trusted_local=True),
                       make_json_client(base_url, model, diagnostics_dir=target / "model-diagnostics"))
        return run.run(start_year, end_year, refresh_fulltext=refresh_fulltext)

    def scoop_status(self, scoop_run_dir):
        target = self._run_dir(scoop_run_dir)
        state = target / "state.json"
        if not state.is_file():
            return {"schema_version": "1.0", "adapter": "scoop-check-v1",
                    "phase": "new", "last_step": 0}
        result = read_json(state)
        if (target / "step7.json").is_file():
            result["report"] = read_json(target / "step7.json")
        return result

    def scoop_to_arbor(self, idea_run_dir, scoop_run_dir, arbor_run_dir,
                       parent="n0", allow_provisional=False):
        scoop = self._run_dir(scoop_run_dir)
        arbor = self._run_dir(arbor_run_dir)
        report_path = scoop / "step7.json"
        if not report_path.is_file():
            raise ProtocolError("Scoop Check 尚未产生最终报告")
        report = read_json(report_path)
        if report.get("status") not in {"completed", "provisional"}:
            raise ProtocolError("Scoop Check 报告状态不合法")
        if report["status"] == "provisional" and not allow_provisional:
            raise ProtocolError("provisional Scoop Check 需要显式 --allow-provisional")
        candidate_paths = [self._run_dir(idea_run_dir) / "phase3_revise" / "final_candidate.json",
                           self._run_dir(idea_run_dir) / "phase2_coherence" / "refined_candidate.json",
                           self._run_dir(idea_run_dir) / "phase2_generate" / "phase2_generate_output.json"]
        current = next((path for path in candidate_paths if path.is_file()), None)
        if current is None or file_hash(current) != report.get("candidate_sha256"):
            raise ProtocolError("Scoop Check 报告与当前 Idea candidate 不一致")
        report_sha = file_hash(report_path)
        links_dir = arbor / ".popper-integration"
        links_path = links_dir / "scoop-arbor-links.json"
        links = read_json(links_path) if links_path.is_file() else {"schema_version": "1.0", "links": []}
        for link in links.get("links", []):
            if link.get("scoop_report_sha256") == report_sha and link.get("parent") == parent:
                return {"adapter": "scoop-to-arbor-v1", "status": "already_linked",
                        "link": link, "state": self.arbor_state(arbor)}
        idea_link = self.idea_to_arbor(idea_run_dir, arbor_run_dir, parent)
        link = {"node_id": idea_link["candidate"]["node_id"], "parent": parent,
                "scoop_status": report["status"], "scoop_report_sha256": report_sha,
                "level": report["level"], "label": report["label"],
                "closest_paper_id": report.get("closest_paper_id"), "delta": report.get("delta"),
                "search_sha256": report.get("search_sha256"),
                "fulltext_sha256": report.get("fulltext_sha256", [])}
        links.setdefault("links", []).append(link)
        links_dir.mkdir(parents=True, exist_ok=True)
        temp = links_path.with_suffix(".tmp")
        temp.write_text(json.dumps(links, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(links_path)
        return {"adapter": "scoop-to-arbor-v1", "status": "linked", "link": link,
                "state": idea_link["state"]}

    def arbor_evaluate(self, arbor_run_dir, experiment_dir, node, candidate_index,
                       trusted_local=False):
        if not trusted_local:
            raise ProtocolError("当前执行非沙箱；arbor-evaluate 需要显式 --trusted-local")
        arbor = self._run_dir(arbor_run_dir)
        experiment_root = Path(experiment_dir).resolve()
        if not experiment_root.is_relative_to(self.workspace_root):
            raise ProtocolError("实验项目必须位于当前工作区")
        tree_state = self.arbor_state(arbor)
        nodes = {item["id"]: item for item in tree_state["nodes"]}
        if node not in nodes or nodes[node]["status"] not in {"pending", "running", "executed"}:
            raise ProtocolError("Arbor node 不存在或状态不允许执行")
        experiment = Experiment(experiment_root)
        try:
            state = experiment.state()
            if state["phase"] != "searching":
                raise ProtocolError("Popper 实验必须处于 searching 阶段")
            spec = state["spec"]
            if not 0 <= candidate_index < len(spec["candidates"]):
                raise ProtocolError("candidate-index 越界")
            if tree_state["run"]["metric_direction"] != spec["metric"]["direction"]:
                raise ProtocolError("Arbor 与 Popper 指标方向不一致")
            candidate = spec["candidates"][candidate_index]
            key = digest({"experiment_inputs": state["input_hashes"], "node": node,
                          "candidate": candidate})
            links_path = arbor / ".popper-integration" / "arbor-popper-runs.json"
            links = read_json(links_path) if links_path.is_file() else {"schema_version": "1.0", "links": []}
            for link in links.get("links", []):
                if link.get("execution_key") == key:
                    return {"adapter": "arbor-popper-dev-v1", "status": "already_evaluated",
                            "link": link, "state": self.arbor_state(arbor)}
            if nodes[node]["status"] == "executed":
                raise ProtocolError("Arbor node 已经写入其他实验的证据")
            baseline = experiment.evaluate(spec["baseline"], "dev", True)
            result = experiment.evaluate(candidate, "dev", True)
            direction = spec["metric"]["direction"]
            delta = (baseline["mean"] - result["mean"] if direction == "min"
                     else result["mean"] - baseline["mean"])
            summary = (f"{spec['metric']['name']}={result['mean']:.8g}; "
                       f"baseline={baseline['mean']:.8g}; oriented_delta={delta:.8g}")
            branch_ref = f"popper-run:{result['run_id']}"
            updated = self.arbor_evidence(arbor, node, result["mean"], summary,
                                          f"开发集控制器计分差值 {delta:.8g}", branch_ref)
            link = {"execution_key": key, "node_id": node, "candidate_index": candidate_index,
                    "config": candidate, "metric": spec["metric"], "dev_score": result["mean"],
                    "baseline_score": baseline["mean"], "oriented_delta": delta,
                    "baseline_run_id": baseline["run_id"], "candidate_run_id": result["run_id"],
                    "experiment": str(experiment_root), "input_hashes": state["input_hashes"]}
            links.setdefault("links", []).append(link)
            links_path.parent.mkdir(parents=True, exist_ok=True)
            temp = links_path.with_suffix(".tmp")
            temp.write_text(json.dumps(links, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(links_path)
            return {"adapter": "arbor-popper-dev-v1", "status": "evaluated",
                    "link": link, "state": updated["state"]}
        finally:
            experiment.close()

    def arbor_dispatch(self, idea_run_dir, scoop_run_dir, arbor_run_dir, experiment_dir,
                       node, base_url=None, model=None, trusted_local=False,
                       allow_provisional=False, llm=None):
        from .scoop import make_json_client
        if not trusted_local:
            raise ProtocolError("当前执行非沙箱；arbor-dispatch 需要显式 --trusted-local")
        idea = self._run_dir(idea_run_dir)
        scoop = self._run_dir(scoop_run_dir)
        arbor = self._run_dir(arbor_run_dir)
        experiment_root = Path(experiment_dir).resolve()
        if not experiment_root.is_relative_to(self.workspace_root):
            raise ProtocolError("实验项目必须位于当前工作区")
        report_path = scoop / "step7.json"
        if not report_path.is_file():
            raise ProtocolError("arbor-dispatch 需要 Scoop Check 报告")
        report = read_json(report_path)
        if report.get("status") == "provisional" and not allow_provisional:
            raise ProtocolError("provisional Scoop Check 需要显式 --allow-provisional")
        if report.get("status") not in {"completed", "provisional"}:
            raise ProtocolError("Scoop Check 报告状态不合法")
        candidate_paths = [idea / "phase3_revise" / "final_candidate.json",
                           idea / "phase2_coherence" / "refined_candidate.json",
                           idea / "phase2_generate" / "phase2_generate_output.json"]
        candidate_path = next((path.resolve() for path in candidate_paths if path.is_file()), None)
        if candidate_path is None or file_hash(candidate_path) != report.get("candidate_sha256"):
            raise ProtocolError("Scoop Check 报告与当前 Idea candidate 不一致")
        candidate = read_json(candidate_path)
        if isinstance(candidate.get("final_candidate"), dict):
            candidate = candidate["final_candidate"]
        experiment = Experiment(experiment_root)
        try:
            state = experiment.state()
            if state["phase"] != "searching":
                raise ProtocolError("Popper 实验必须处于 searching 阶段")
            request = {"idea": candidate, "scoop": {key: report.get(key) for key in
                       ("status", "level", "label", "delta", "closest_paper_id")},
                       "objective": state["spec"]["objective"],
                       "metric": state["spec"]["metric"],
                       "registered_candidates": state["spec"]["candidates"]}
            execution_key = digest({"candidate_sha256": file_hash(candidate_path),
                                    "scoop_sha256": file_hash(report_path), "node": node,
                                    "experiment_inputs": state["input_hashes"]})
            mappings_path = arbor / ".popper-integration" / "dispatches.json"
            mappings = (read_json(mappings_path) if mappings_path.is_file()
                        else {"schema_version": "1.0", "dispatches": []})
            mapping = next((item for item in mappings.get("dispatches", [])
                            if item.get("execution_key") == execution_key), None)
            if mapping is None:
                client = llm or make_json_client(base_url, model, diagnostics_dir=arbor / "model-diagnostics")
                decision = client(
                    "Map the research idea to one registered experiment configuration. Return JSON with implementable (boolean), candidate_index (integer or null), rationale (non-empty string), and expected_effect (non-empty string). Set implementable=false when no registered configuration tests the idea. Never invent a configuration.",
                    request)
                if (type(decision.get("implementable")) is not bool
                        or not isinstance(decision.get("rationale"), str) or not decision["rationale"].strip()
                        or not isinstance(decision.get("expected_effect"), str) or not decision["expected_effect"].strip()):
                    raise ProtocolError("arbor-dispatch 模型响应不符合契约")
                index = decision.get("candidate_index")
                if decision["implementable"]:
                    if type(index) is not int or not 0 <= index < len(state["spec"]["candidates"]):
                        raise ProtocolError("arbor-dispatch 返回未注册 candidate-index")
                elif index is not None:
                    raise ProtocolError("不可实现的 dispatch 必须使用 candidate_index=null")
                mapping = {"execution_key": execution_key, "model": model or "injected-test-client",
                           "candidate_sha256": file_hash(candidate_path),
                           "scoop_report_sha256": file_hash(report_path), **decision}
                mappings.setdefault("dispatches", []).append(mapping)
                mappings_path.parent.mkdir(parents=True, exist_ok=True)
                temp = mappings_path.with_suffix(".tmp")
                temp.write_text(json.dumps(mappings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temp.replace(mappings_path)
            if not mapping["implementable"]:
                return {"adapter": "arbor-dispatch-v1", "status": "not_implementable",
                        "mapping": mapping, "state": self.arbor_state(arbor)}
            evaluated = self.arbor_evaluate(arbor, experiment_root, node,
                                            mapping["candidate_index"], trusted_local)
            return {"adapter": "arbor-dispatch-v1", "status": evaluated["status"],
                    "mapping": mapping, "evaluation": evaluated["link"],
                    "state": evaluated["state"]}
        finally:
            experiment.close()

    def research_snapshot(self, arbor_run_dir, experiment_dir):
        arbor = self._run_dir(arbor_run_dir)
        experiment_root = Path(experiment_dir).resolve()
        if not experiment_root.is_relative_to(self.workspace_root):
            raise ProtocolError("实验项目必须位于当前工作区")
        self.inspect()
        tree = self.arbor_state(arbor)
        experiment = Experiment(experiment_root)
        try:
            exp_state = experiment.state()
            results = experiment.results("dev")
        finally:
            experiment.close()
        links_path = arbor / ".popper-integration" / "arbor-popper-runs.json"
        links = read_json(links_path).get("links", []) if links_path.is_file() else []
        by_node = {link["node_id"]: link for link in links}
        entries = [{"type": "bootstrap", "summary": tree["run"]["objective"]}]
        key_results = []
        lessons = []
        for node in tree["nodes"]:
            if node["id"] == tree["root"]["id"]:
                continue
            link = by_node.get(node["id"])
            if link:
                summary = (f"{node['id']} {node['hypothesis']}: {link['metric']['name']}="
                           f"{link['dev_score']:.8g}, baseline={link['baseline_score']:.8g}, "
                           f"oriented_delta={link['oriented_delta']:.8g}; run={link['candidate_run_id']}")
                entries.append({"type": "inner-loop", "summary": summary})
                key_results.append(summary)
            if node.get("insight"):
                lessons.append(f"{node['id']}: {node['insight']}")
        if tree["root"].get("insight"):
            entries.append({"type": "outer-loop", "summary": tree["root"]["insight"]})
        pending = [f"{node['id']}: {node['hypothesis']}" for node in tree["frontier"]]
        trajectory = [{"run_id": result["run_id"], "config": result["config"],
                       "score": result["mean"], "metric": result["metric"]} for result in results]
        snapshot = {"schema_version": "1.0", "adapter": "autoresearch-snapshot-v1",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "objective": tree["run"]["objective"], "experiment_phase": exp_state["phase"],
                    "entries": entries, "key_results": key_results, "lessons": lessons,
                    "open_questions": pending, "trajectory": trajectory,
                    "source_hashes": {"arbor_tree": file_hash(arbor / ".arbor" / "tree.json"),
                                      "arbor_run": file_hash(arbor / ".arbor" / "run.json"),
                                      "experiment": exp_state["input_hashes"]}}
        output_dir = arbor / ".popper-integration" / "autoresearch"
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "snapshot.json"
        json_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        today = snapshot["generated_at"][:10]
        log = ["# Research Log", "", "| # | Date | Type | Summary |", "|---|------|------|---------|"]
        for index, entry in enumerate(entries, 1):
            safe = entry["summary"].replace("|", "\\|").replace("\n", " ")
            log.append(f"| {index} | {today} | {entry['type']} | {safe} |")
        (output_dir / "research-log.md").write_text("\n".join(log) + "\n", encoding="utf-8")
        findings = ["# Research Findings", "", "## Research Question", "", snapshot["objective"], "",
                    "## Current Understanding", "", tree["root"].get("insight") or "尚无已传播到根节点的综合洞见。", "",
                    "## Key Results", "", *([f"- {x}" for x in key_results] or ["- 尚无已完成的节点实验。"]), "",
                    "## Patterns and Insights", "", *([f"- {x}" for x in lessons] or ["- 尚无。"]), "",
                    "## Lessons and Constraints", "", *([f"- {x}" for x in lessons] or ["- 尚无。"]), "",
                    "## Open Questions", "", *([f"- {x}" for x in pending] or ["- 当前 frontier 为空。"]), "",
                    "## Optimization Trajectory", "",
                    *([f"- {x['run_id']}: {x['metric']['name']}={x['score']:.8g}; config={json.dumps(x['config'], ensure_ascii=False, sort_keys=True)}" for x in trajectory]
                      or ["- 尚无开发集运行。"]), ""]
        (output_dir / "findings.md").write_text("\n".join(findings), encoding="utf-8")
        return {"adapter": "autoresearch-snapshot-v1", "status": "generated",
                "snapshot": str(json_path), "research_log": str(output_dir / "research-log.md"),
                "findings": str(output_dir / "findings.md"), "counts": {
                    "entries": len(entries), "results": len(key_results), "open_questions": len(pending)}}

    def code_propose(self, idea_run_dir, scoop_run_dir, proposal_run_dir,
                     experiment_dir, base_url=None, model=None, llm=None):
        from .code_proposal import propose_code
        from .scoop import make_json_client
        idea = self._run_dir(idea_run_dir)
        scoop = self._run_dir(scoop_run_dir)
        proposal = self._run_dir(proposal_run_dir)
        experiment = Path(experiment_dir).resolve()
        if not experiment.is_relative_to(self.workspace_root):
            raise ProtocolError("实验项目必须位于当前工作区")
        self.inspect()
        component, _ = self.component("ai_research_ml_training")
        skill = self.resolve("ai_research_ml_training", component["skill_file"])
        client = llm or make_json_client(base_url, model, diagnostics_dir=proposal / "model-diagnostics")
        return propose_code(idea, scoop, proposal, experiment, skill,
                            model or "injected-test-client", client)

    def code_materialize(self, proposal_run_dir, experiment_dir, config_index=0,
                         approved=False):
        from .code_variant import materialize
        proposal = self._run_dir(proposal_run_dir)
        experiment = Path(experiment_dir).resolve()
        if not experiment.is_relative_to(self.workspace_root):
            raise ProtocolError("实验项目必须位于当前工作区")
        return materialize(proposal, experiment, proposal / "variant-project",
                           config_index, approved)

    def idea_next(self, run_dir, query=None, base_url=None, model=None, evaluation_contract=None):
        target = self._run_dir(run_dir)
        args = ["next", "--dir", target]
        if query:
            args.extend(["--query", query])
        result = self._execute("researchstudio_idea", args)
        navigation = {}
        current = None
        for raw in result["stdout"].splitlines():
            line = raw.rstrip()
            if not line or set(line) == {"━"}:
                continue
            if ":" in line:
                label, value = line.split(":", 1)
                key = label.strip().lower()
                if key in {"state", "step", "type", "do", "prompt", "input", "output", "run", "notes", "then"}:
                    current = key
                    navigation[key] = value.strip()
                    continue
            if current:
                navigation[current] += "\n" + line
        if not {"state", "step", "type"} <= set(navigation):
            raise ProtocolError("ResearchStudio next 输出缺少 STATE/STEP/TYPE")
        result.update({"run_dir": str(target), "adapter": "idea-next-v1",
                       "navigation": navigation})
        # Idea Spark 的 LLM 子任务自动执行器（有界实现）：TYPE=llm_subagent 且提供
        # BYOK LLM 时，把候选落地为 canonical final_candidate.json。
        process = self._execute_llm_subagent(target, navigation,
                                             base_url, model, query or "", evaluation_contract)
        if process is not None:
            result["process"] = process
        return result

    def _execute_llm_subagent(self, target, navigation, base_url, model, query, evaluation_contract=None):
        """有界 Idea 子任务执行器。

        读取目标/研究问题，用 BYOK LLM 生成 canonical candidate 并写入
        phase3_revise/final_candidate.json。失败或未提供 LLM 时返回 None（保持手动）。
        """
        if not (base_url and model) or navigation.get("type") != "llm_subagent":
            if navigation.get("type") == "llm_subagent":
                return {"process": "manual",
                        "note": "导航器 Type=llm_subagent；未提供 --base-url/--model，需人工/LLM 补全"}
            return {"process": "manual", "note": "当前导航项非 llm_subagent"}
        from .scoop import make_json_client
        try:
            from .candidate_contract import validate_candidate
            client = make_json_client(base_url, model, diagnostics_dir=target / "model-diagnostics")
            prompt = ("Given this research question, produce a canonical research candidate as JSON. "
                      "Return keys title, core_mechanism, falsification_prediction.")
            payload = {"research_question": query}
            if evaluation_contract is not None:
                prompt += (" Also return evaluation_contract copied exactly from the supplied contract. "
                           "Use only its metric, baseline and absolute improvement threshold. The prediction "
                           "must be testable by this evaluator. Do not introduce probability metrics, cross-validation, "
                           "statistical significance or confidence intervals. This is a descriptive fixed-split experiment.")
                payload["evaluation_contract"] = evaluation_contract
            for attempt in range(2):
                candidate = client(prompt, payload)
                try:
                    if evaluation_contract is not None:
                        validate_candidate(candidate, evaluation_contract)
                    elif not all(isinstance(candidate.get(k), str) and candidate[k].strip()
                                 for k in ("title", "core_mechanism", "falsification_prediction")):
                        raise ProtocolError("LLM 候选不符合契约")
                    break
                except ProtocolError as error:
                    if attempt == 1:
                        raise
                    payload["validation_error"] = str(error)
                    payload["rejected_candidate"] = candidate
            out_dir = target / "phase3_revise"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "final_candidate.json"
            out_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
            return {"process": "automated", "candidate": str(out_path),
                    "sha256": file_hash(out_path)}
        except Exception as error:
            if evaluation_contract is not None:
                raise ProtocolError(f"Idea 候选生成或契约校验失败：{type(error).__name__}") from None
            return {"process": "manual", "note": f"LLM 子任务失败：{type(error).__name__}"}

    def arbor_init(self, run_dir, objective, dev_eval, test_eval, material=".",
                   metric_direction="max", branching=3, max_depth=2, budget=12):
        target = self._run_dir(run_dir)
        target.mkdir(parents=True, exist_ok=True)
        args = ["--run-dir", target, "init", "--objective", objective,
                "--dev-eval", dev_eval, "--test-eval", test_eval, "--material", material,
                "--metric-direction", metric_direction, "--branching", branching,
                "--max-depth", max_depth, "--budget", budget]
        result = self._execute("arbor", args)
        result.update({"run_dir": str(target), "adapter": "arbor-tree-v1"})
        return result

    def arbor_read(self, run_dir, command="observe"):
        if command not in {"observe", "status", "validate"}:
            raise ProtocolError("只允许 Arbor 只读命令 observe/status/validate")
        target = self._run_dir(run_dir)
        result = self._execute("arbor", ["--run-dir", target, command])
        result.update({"run_dir": str(target), "adapter": "arbor-tree-v1", "command": command})
        return result

    def arbor_state(self, run_dir):
        target = self._run_dir(run_dir)
        self.arbor_read(target, "validate")
        tree = read_json(target / ".arbor" / "tree.json")
        run = read_json(target / ".arbor" / "run.json")
        frontier = [node for node in tree["nodes"].values() if node["status"] == "pending"]
        evidence = [node for node in tree["nodes"].values()
                    if node["status"] in {"executed", "merged", "pruned"}]
        return {"adapter": "arbor-tree-v1", "run_dir": str(target), "run": run,
                "root": tree["nodes"][tree["root"]], "nodes": list(tree["nodes"].values()),
                "frontier": frontier, "evidence": evidence}

    def _arbor_action(self, run_dir, command, args):
        target = self._run_dir(run_dir)
        result = self._execute("arbor", ["--run-dir", target, command, *args])
        result.update({"run_dir": str(target), "adapter": "arbor-tree-v1",
                       "command": command, "state": self.arbor_state(target)})
        return result

    def arbor_add(self, run_dir, parent, hypothesis):
        return self._arbor_action(run_dir, "add-node",
                                  ["--parent", parent, "--hypothesis", hypothesis])

    def arbor_evidence(self, run_dir, node, dev_score, result, insight, branch_ref):
        return self._arbor_action(
            run_dir, "set-evidence",
            ["--node", node, "--dev-score", dev_score, "--result", result,
             "--insight", insight, "--branch-ref", branch_ref])

    def arbor_propagate(self, run_dir, node, insight, to_root=False):
        args = ["--node", node, "--insight", insight]
        if to_root:
            args.append("--to-root")
        return self._arbor_action(run_dir, "propagate", args)

    def arbor_prune(self, run_dir, node, reason):
        return self._arbor_action(run_dir, "prune", ["--node", node, "--reason", reason])

    def arbor_merge(self, run_dir, node, test_score, branch_ref):
        return self._arbor_action(run_dir, "merge",
                                  ["--node", node, "--test-score", test_score,
                                   "--branch-ref", branch_ref])

    def arbor_cycle(self, run_dir):
        return self._arbor_action(run_dir, "cycle", [])

    def idea_to_arbor(self, idea_run_dir, arbor_run_dir, parent="n0"):
        idea_run = self._run_dir(idea_run_dir)
        arbor_run = self._run_dir(arbor_run_dir)
        candidates = [
            idea_run / "phase3_revise" / "final_candidate.json",
            idea_run / "phase2_coherence" / "refined_candidate.json",
            idea_run / "phase2_generate" / "phase2_generate_output.json",
        ]
        candidate_path = next((path.resolve() for path in candidates if path.is_file()), None)
        if candidate_path is None:
            raise ProtocolError("Idea 运行目录中没有可桥接的 canonical candidate")
        if not candidate_path.is_relative_to(idea_run):
            raise ProtocolError("Idea candidate 路径越界")
        candidate = read_json(candidate_path)
        if isinstance(candidate, dict) and isinstance(candidate.get("final_candidate"), dict):
            candidate = candidate["final_candidate"]
        if not isinstance(candidate, dict):
            raise ProtocolError("Idea candidate 必须是 JSON 对象")
        title = candidate.get("title")
        prediction = candidate.get("falsification_prediction")
        if not isinstance(title, str) or not title.strip():
            raise ProtocolError("Idea candidate 缺少 title")
        if not isinstance(prediction, str) or not prediction.strip():
            raise ProtocolError("Idea candidate 缺少 falsification_prediction")

        candidate_sha = file_hash(candidate_path)
        links_dir = arbor_run / ".popper-integration"
        links_path = links_dir / "idea-arbor-links.json"
        links = read_json(links_path) if links_path.is_file() else {"schema_version": "1.0", "links": []}
        if (not isinstance(links, dict) or links.get("schema_version") != "1.0"
                or not isinstance(links.get("links"), list)):
            raise ProtocolError("Idea→Arbor 桥接记录不符合 1.0 契约")
        for link in links["links"]:
            if link.get("candidate_sha256") == candidate_sha and link.get("parent") == parent:
                return {"adapter": "idea-to-arbor-v1", "status": "already_linked",
                        "idea_run_dir": str(idea_run), "arbor_run_dir": str(arbor_run),
                        "candidate": link, "state": self.arbor_state(arbor_run)}

        hypothesis = f"{title.strip()}: {prediction.strip()}"
        before = {node["id"] for node in self.arbor_state(arbor_run)["nodes"]}
        added = self.arbor_add(arbor_run, parent, hypothesis)
        created = [node for node in added["state"]["nodes"] if node["id"] not in before]
        if len(created) != 1:
            raise ProtocolError("Arbor 未返回唯一的新节点")
        link = {"node_id": created[0]["id"], "parent": parent,
                "candidate_path": str(candidate_path.relative_to(idea_run)),
                "candidate_sha256": candidate_sha, "title": title.strip(),
                "falsification_prediction": prediction.strip()}
        links["links"].append(link)
        links_dir.mkdir(parents=True, exist_ok=True)
        temporary = links_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(links, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(links_path)
        return {"adapter": "idea-to-arbor-v1", "status": "linked",
                "idea_run_dir": str(idea_run), "arbor_run_dir": str(arbor_run),
                "candidate": link, "state": added["state"]}
