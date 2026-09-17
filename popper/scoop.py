"""Stateful Popper orchestration of ResearchStudio's scoop-check protocol."""
import hashlib
import json
import os
import re
import uuid
import shutil
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .core import ProtocolError, canonical, file_hash, read_json
from .fulltext import fetch_paper_text


AXES = ("problem_framing", "core_mechanism", "key_insight", "application_domain")

# 单次 LLM triage 的数量上限：论文过多时一次输出会截断，故仅在 step3 取相关性最高的 Top-N。
TRIAGE_CAP = 30


def make_json_client(base_url, model, diagnostics_dir=None):
    parsed = urlparse(base_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ProtocolError("模型地址必须是 HTTPS，或本机 HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProtocolError("模型地址不能含凭据、查询参数或 fragment")
    key = os.environ.get("POPPER_API_KEY")
    if not key:
        raise ProtocolError("请通过 POPPER_API_KEY 环境变量提供 key")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ProtocolError("模型接口重定向被拒绝")
    opener = urllib.request.build_opener(NoRedirect)

    def call(system, payload):
        call_id = uuid.uuid4().hex
        repair_json = False
        for attempt in range(2):
            body = {"model": model, "temperature": 0, "max_tokens": 16000 if repair_json else 8000,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": canonical(payload)}]}
            if repair_json:
                body["messages"].append({"role": "user", "content":
                    "The previous response was truncated or invalid JSON. Return one complete JSON "
                    "object matching the requested schema. Keep explanations concise; no Markdown."})
            request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                             data=canonical(body).encode(), method="POST",
                                             headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            diagnostic = {"call_id": call_id, "attempt": attempt + 1, "model": model,
                          "request_sha256": hashlib.sha256(canonical(body).encode()).hexdigest(),
                          "max_tokens": body["max_tokens"]}
            retryable = False
            try:
                with opener.open(request, timeout=120) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ProtocolError("模型响应超过大小上限")
                text = raw.decode("utf-8")
                diagnostic["response"] = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", text.replace(key, "[REDACTED]"))
                retryable = True
                envelope = json.loads(text)
                choice = envelope["choices"][0]
                diagnostic["finish_reason"] = choice.get("finish_reason")
                diagnostic["usage"] = envelope.get("usage")
                if choice.get("finish_reason") == "length":
                    raise ValueError("truncated response")
                result = json.loads(choice["message"]["content"])
                if not isinstance(result, dict):
                    raise ValueError("JSON object required")
                diagnostic["status"] = "success"
                return result
            except Exception as error:
                repair_json = retryable
                diagnostic["status"] = "failed"
                diagnostic["error_type"] = type(error).__name__
                diagnostic["http_status"] = getattr(error, "code", None)
                if isinstance(error, urllib.error.HTTPError):
                    retryable = error.code in {429, 502, 503, 504}
                    try:
                        error_text = error.read(16_384).decode("utf-8", errors="replace")
                        diagnostic["error_response"] = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]",
                                                              error_text.replace(key, "[REDACTED]"))
                    except Exception:
                        pass
                elif isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)):
                    retryable = True
                    diagnostic["network_reason_type"] = type(getattr(error, "reason", error)).__name__
                if attempt == 1 or not retryable:
                    raise ProtocolError(f"Scoop 模型调用失败: {type(error).__name__}；"
                                        f"诊断编号 {call_id}，尝试 {attempt + 1} 次") from None
            finally:
                if diagnostics_dir is not None:
                    folder = Path(diagnostics_dir)
                    folder.mkdir(parents=True, exist_ok=True)
                    serialized = json.dumps(diagnostic, ensure_ascii=False, indent=2)
                    serialized = serialized.replace(key, "[REDACTED]")
                    (folder / f"{call_id}-{attempt + 1}.json").write_text(serialized + "\n", encoding="utf-8")

    return call


def _paper_id(paper):
    key = paper.get("doi") or paper.get("arxiv_id") or paper.get("url") or paper.get("title", "")
    return "P-" + hashlib.sha256(str(key).encode()).hexdigest()[:12]


def _pdf_url(paper):
    url = str(paper.get("url") or "")
    if "arxiv.org/abs/" in url:
        url = url.replace("http://", "https://", 1)
        return url.replace("/abs/", "/pdf/") + ("" if url.endswith(".pdf") else ".pdf")
    return url if url.lower().split("?", 1)[0].endswith(".pdf") else None


def fetch_pdf_text(url, output_dir, paper_id):
    if not url or urlparse(url).scheme != "https":
        raise ProtocolError("论文没有可用的 HTTPS PDF 地址")
    request = urllib.request.Request(url, headers={"User-Agent": "Popper/0.1 research verification"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(50_000_001)
    if len(data) > 50_000_000 or not data.startswith(b"%PDF-"):
        raise ProtocolError("下载内容不是有效的受限大小 PDF")
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf = output_dir / f"{paper_id}.pdf"
    text = output_dir / f"{paper_id}.txt"
    pdf.write_bytes(data)
    completed = subprocess.run(["pdftotext", "-layout", str(pdf), str(text)],
                               capture_output=True, text=True, timeout=60, check=False)
    if completed.returncode or not text.is_file() or len(text.read_text(encoding="utf-8", errors="replace").strip()) < 200:
        raise ProtocolError("PDF 全文提取失败或内容不足")
    return {"pdf": str(pdf), "pdf_sha256": file_hash(pdf), "text": str(text),
            "text_sha256": file_hash(text), "content": text.read_text(encoding="utf-8", errors="replace")[:60000]}


def _verify_fulltext(llm, candidate, paper, content):
    payload = {"candidate": candidate, "paper": paper, "text": content}
    prompt = ("Verify the four axes from this paper text. Return JSON with paper_id, "
              "axis_matches object using exactly problem_framing, core_mechanism, key_insight, application_domain "
              "and match/partial/differ values, assumptions_scope, closest_passage. "
              "closest_passage must be an exact consecutive quotation of 20 to 300 characters from the supplied text. "
              "Do not paraphrase or join non-adjacent text from PDF columns.")
    normalize = lambda text: " ".join(text.split())
    for attempt in range(2):
        verified = llm(prompt, payload)
        matches = verified.get("axis_matches", {})
        passage = verified.get("closest_passage", "")
        if (verified.get("paper_id") == paper["paper_id"] and isinstance(matches, dict)
                and set(matches) == set(AXES)
                and all(value in {"match", "partial", "differ"} for value in matches.values())
                and isinstance(passage, str) and 20 <= len(passage.strip()) <= 300
                and normalize(passage) in normalize(content)):
            return {"axis_matches": matches, "assumptions_scope": verified.get("assumptions_scope"),
                    "closest_passage": passage}
        if attempt == 0:
            payload = {**payload, "validation_error": "Paper id, axes or literal quotation did not match. "
                       "Copy a shorter consecutive passage directly from the supplied text.",
                       "rejected_response": verified}
    raise ProtocolError("全文核验响应未绑定论文、指标轴或真实原文片段（两次尝试）")


def _validate_comparisons(comparison, selected):
    comparisons = comparison.get("comparisons") if isinstance(comparison, dict) else None
    if (not isinstance(comparisons, list) or len(comparisons) != len(selected)
            or any(not isinstance(item, dict) for item in comparisons)
            or {item.get("paper_id") for item in comparisons} != set(selected)):
        raise ProtocolError("Scoop comparisons 必须完整且不重复地覆盖选中论文")
    closest = comparison.get("closest_paper_id")
    if (selected and closest not in selected) or (not selected and closest is not None):
        raise ProtocolError("closest_paper_id 必须引用选中论文")
    for item in comparisons:
        matches = item.get("axis_matches", {})
        if (not isinstance(matches, dict) or set(matches) != set(AXES)
                or any(v not in {"match", "partial", "differ"} for v in matches.values())):
            raise ProtocolError("Scoop comparison axis_matches 不符合契约")


class ScoopRun:
    def __init__(self, idea_dir, run_dir, search, llm, fetch=fetch_pdf_text):
        self.idea_dir, self.run_dir = Path(idea_dir).resolve(), Path(run_dir).resolve()
        self.search, self.llm, self.fetch = search, llm, fetch
        self._resolve_paper = fetch is fetch_pdf_text
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.run_dir / "state.json"

    def _candidate(self):
        choices = [self.idea_dir / "phase3_revise" / "final_candidate.json",
                   self.idea_dir / "phase2_coherence" / "refined_candidate.json",
                   self.idea_dir / "phase2_generate" / "phase2_generate_output.json"]
        path = next((p.resolve() for p in choices if p.is_file()), None)
        if not path or not path.is_relative_to(self.idea_dir):
            raise ProtocolError("Idea 运行目录中没有可用 candidate")
        value = read_json(path)
        if isinstance(value.get("final_candidate"), dict):
            value = value["final_candidate"]
        required = ("title", "core_mechanism", "falsification_prediction")
        if not all(isinstance(value.get(k), str) and value[k].strip() for k in required):
            raise ProtocolError("candidate 缺少 title/core_mechanism/falsification_prediction")
        return path, value

    def _save_step(self, number, value, phase):
        path = self.run_dir / f"step{number}.json"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        state = {"schema_version": "1.0", "adapter": "scoop-check-v1", "phase": phase,
                 "last_step": number, "updated_at": datetime.now(timezone.utc).isoformat()}
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return value

    def status(self):
        if not self.state_path.is_file():
            return {"schema_version": "1.0", "adapter": "scoop-check-v1", "phase": "new", "last_step": 0}
        state = read_json(self.state_path)
        if (self.run_dir / "step7.json").is_file():
            state["report"] = read_json(self.run_dir / "step7.json")
        return state

    def run(self, start_year, end_year, refresh_fulltext=False):
        try:
            return self._run(start_year, end_year, refresh_fulltext)
        except Exception as error:
            previous = read_json(self.state_path) if self.state_path.is_file() else {"last_step": 0}
            failure = {"schema_version": "1.0", "adapter": "scoop-check-v1", "phase": "failed",
                       "last_step": previous.get("last_step", 0), "error": type(error).__name__,
                       "updated_at": datetime.now(timezone.utc).isoformat()}
            self.state_path.write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise

    def _run(self, start_year, end_year, refresh_fulltext=False):
        candidate_path, candidate = self._candidate()
        manifest_path = self.run_dir / "run.json"
        manifest = {"schema_version": "1.0", "candidate_sha256": file_hash(candidate_path),
                    "start_year": start_year, "end_year": end_year}
        if manifest_path.is_file() and read_json(manifest_path) != manifest:
            raise ProtocolError("Scoop run 输入与已保存运行不一致")
        if not manifest_path.is_file():
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path = self.run_dir / "step7.json"
        report = read_json(report_path) if report_path.is_file() else {}
        outdated = report.get("status") == "provisional" and report.get("fulltext_policy") != "public-pdf-v2"
        if refresh_fulltext or outdated:
            # Preserve old verdicts and artifacts before invalidating only dependent steps.
            revision = self.run_dir / "history" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
            revision.mkdir(parents=True, exist_ok=True)
            for name in ("step5.json", "step6.json", "step7.json", "state.json"):
                path = self.run_dir / name
                if path.is_file():
                    shutil.copyfile(path, revision / name)
                    if name != "state.json":
                        path.unlink()
            # Artifact names below a new revision preserve earlier hash-bound evidence.
            (self.run_dir / "fulltext-revision.json").write_text(
                json.dumps({"revision": revision.name}) + "\n", encoding="utf-8")
        elif report_path.is_file():
            return self.status()
        if not (self.run_dir / "step1.json").is_file():
            step1 = self.llm("Decompose the novelty into exactly these four named axes: problem_framing, core_mechanism, key_insight, application_domain. Create exactly three focused paper queries. Return JSON: axes object whose keys are exactly those four names, and queries array.", candidate)
            if set(step1.get("axes", {})) != set(AXES) or len(step1.get("queries", [])) != 3:
                raise ProtocolError("Scoop step1 JSON 不符合契约")
            self._save_step(1, step1, "decomposed")
        step1 = read_json(self.run_dir / "step1.json")
        if not (self.run_dir / "step2.json").is_file():
            search = self.search(step1["queries"], start_year, end_year)
            self._save_step(2, search, "searched")
        search = read_json(self.run_dir / "step2.json")
        papers = []
        for paper in search.get("papers", []):
            item = dict(paper); item["paper_id"] = _paper_id(paper); papers.append(item)
        if not (self.run_dir / "step3.json").is_file():
            ranked_papers = list(papers)
            if len(ranked_papers) > TRIAGE_CAP:
                ranked_papers = sorted(
                    ranked_papers,
                    key=lambda p: sum(float(p.get(k) or 0) for k in ("citation_count", "relevance_score")),
                    reverse=True)[:TRIAGE_CAP]
            compact = [{k: p.get(k) for k in ("paper_id", "title", "abstract", "year", "url")}
                       for p in ranked_papers]
            triaged = []
            # Small independently checkpointed batches avoid long truncated JSON responses.
            batches = [compact[i:i + 5] for i in range(0, len(compact), 5)] or [[]]
            for batch in batches:
                payload = {"candidate": candidate, "axes": step1["axes"], "papers": batch}
                fingerprint = hashlib.sha256(canonical(payload).encode()).hexdigest()
                cache = self.run_dir / "triage-batches" / (fingerprint + ".json")
                step3 = read_json(cache) if cache.is_file() else self.llm(
                    "Triage every paper against the four named axes problem_framing, core_mechanism, "
                    "key_insight, application_domain. Return JSON papers array; each item needs paper_id, "
                    "one concise text field per axis using exactly those four names, and integer overlap_score 0..4.", payload)
                items = step3.get("papers")
                expected_ids = {p["paper_id"] for p in batch}
                if (not isinstance(items, list) or len(items) != len(batch)
                        or any(not isinstance(p, dict) for p in items)
                        or {p.get("paper_id") for p in items} != expected_ids
                        or any(type(p.get("overlap_score")) is not int or not 0 <= p["overlap_score"] <= 4
                               or not set(AXES) <= set(p) for p in items)):
                    raise ProtocolError("Scoop step3 JSON 不符合契约")
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(step3, ensure_ascii=False) + "\n", encoding="utf-8")
                triaged.extend(items)
            step3 = {"papers": triaged}
            self._save_step(3, step3, "triaged")
        triage = read_json(self.run_dir / "step3.json")["papers"]
        if not (self.run_dir / "step4.json").is_file():
            ranked = sorted(triage, key=lambda p: int(p.get("overlap_score", 0)), reverse=True)
            selected = [p["paper_id"] for p in ranked if int(p.get("overlap_score", 0)) >= 2][:7]
            if len(selected) < min(3, len(ranked)):
                selected = [p["paper_id"] for p in ranked[:min(3, len(ranked))]]
            self._save_step(4, {"candidate_paper_ids": selected}, "selected")
        selected = read_json(self.run_dir / "step4.json")["candidate_paper_ids"]
        by_id = {p["paper_id"]: p for p in papers}
        if not (self.run_dir / "step5.json").is_file():
            records = []
            artifact_dir = self.run_dir / "papers"
            revision_path = self.run_dir / "fulltext-revision.json"
            if revision_path.is_file():
                revision = read_json(revision_path)["revision"]
                if not re.fullmatch(r"[A-Za-z0-9-]+", revision):
                    raise ProtocolError("全文修订目录不合法")
                artifact_dir = artifact_dir / revision
            for pid in selected:
                paper = by_id.get(pid, {})
                try:
                    fetched = (fetch_paper_text(paper, artifact_dir, pid) if self._resolve_paper else
                               self.fetch(_pdf_url(paper), artifact_dir, pid))
                    content = fetched.pop("content")
                    verified = _verify_fulltext(self.llm, candidate, paper, content)
                    records.append({"paper_id": pid, "access": "fulltext", "artifacts": fetched, **verified})
                except Exception as error:
                    records.append({"paper_id": pid, "access": "abstract_only", "error": type(error).__name__,
                                    "message": str(error)[:500], "resolution_attempts": getattr(error, "attempts", [])})
            self._save_step(5, {"papers": records}, "deep_dived")
        deep = read_json(self.run_dir / "step5.json")["papers"]
        if not (self.run_dir / "step6.json").is_file():
            comparison = self.llm("Compare the proposed work with every selected paper. Return JSON comparisons array with paper_id, axis_matches object whose keys are exactly problem_framing, core_mechanism, key_insight, application_domain and whose four values are match/partial/differ, and closest_paper_id.", {"candidate": candidate, "axes": step1["axes"], "selected_paper_ids": selected,
                "triage": [p for p in triage if p["paper_id"] in selected], "deep_dive": deep})
            _validate_comparisons(comparison, selected)
            comparisons = comparison["comparisons"]
            for item in comparisons:
                matches = item.get("axis_matches", {})
                if set(matches) != set(AXES) or any(v not in {"match", "partial", "differ"} for v in matches.values()):
                    raise ProtocolError("Scoop comparison axis_matches 不符合契约")
                item["axes_matching"] = sum(v == "match" for v in matches.values())
                item["level"] = 5 - item["axes_matching"]
            self._save_step(6, comparison, "compared")
        comparison = read_json(self.run_dir / "step6.json")
        _validate_comparisons(comparison, selected)
        if not (self.run_dir / "step7.json").is_file():
            levels = [p["level"] for p in comparison["comparisons"]]
            level = min(levels) if levels else 5
            delta = self.llm("Write one concrete sentence distinguishing the candidate from the closest paper. Return JSON with delta only.", {"candidate": candidate, "comparison": comparison})
            complete = bool(deep) and all(p["access"] == "fulltext" for p in deep)
            report = {"status": "completed" if complete else "provisional", "fulltext_policy": "public-pdf-v2", "level": level,
                      "label": {1:"Full Overlap",2:"High Overlap",3:"Medium Overlap",4:"Low Overlap",5:"No Overlap"}[level],
                      "delta": delta.get("delta", ""), "closest_paper_id": comparison.get("closest_paper_id"),
                      "candidate_path": str(candidate_path.relative_to(self.idea_dir)),
                      "candidate_sha256": file_hash(candidate_path), "comparisons": comparison["comparisons"],
                      "search_sha256": file_hash(self.run_dir / "step2.json"),
                      "fulltext_sha256": [a for p in deep for a in [p.get("artifacts", {}).get("text_sha256")] if a]}
            self._save_step(7, report, report["status"])
        return self.status()
