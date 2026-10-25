"""Controlled Scoop overlap FPR harness (offline fixture LLM).

Runs ScoopRun with injected search/llm/fetch against a frozen fixture corpus.
Reports ``controlled_scoop_overlap_fpr`` separate from A3 RejectionReview FPR
and from quote-binding hallucination rates. Does not set ``real_novelty_fpr``.
Not an audit-grade scoop product.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .core import ProtocolError, read_json, write_json
from .scoop import AXES, ScoopRun, _paper_id


LEVEL_FALSE_POSITIVE_MAX = 3  # Medium Overlap or stronger


def _load_case(case_dir: Path):
    case_dir = Path(case_dir).resolve()
    gold = read_json(case_dir / "gold.json")
    if not isinstance(gold, dict) or type(gold.get("material_overlap")) is not bool:
        raise ProtocolError(f"gold.json 缺少 material_overlap: {case_dir}")
    candidate = read_json(case_dir / "candidate.json")
    papers = read_json(case_dir / "papers.json")
    if not isinstance(papers, list) or not papers:
        raise ProtocolError(f"papers.json 必须是非空数组: {case_dir}")
    fulltexts = {}
    ft_dir = case_dir / "fulltext"
    if ft_dir.is_dir():
        for path in ft_dir.iterdir():
            if path.suffix == ".txt":
                fulltexts[path.stem] = path.read_text(encoding="utf-8")
    ft_map = case_dir / "fulltext.json"
    if ft_map.is_file():
        loaded = read_json(ft_map)
        if not isinstance(loaded, dict):
            raise ProtocolError("fulltext.json 必须是对象")
        fulltexts.update({str(k): str(v) for k, v in loaded.items()})
    return {
        "id": case_dir.name,
        "dir": case_dir,
        "gold": gold,
        "candidate": candidate,
        "papers": papers,
        "fulltexts": fulltexts,
    }


def _bound_passage(content: str) -> str:
    text = " ".join(str(content).split())
    if len(text) < 40:
        raise ProtocolError("fixture 全文过短，无法构造绑定引文")
    return text[:40]


def _make_llm(profile: str, paper_ids: list[str], contents: dict[str, str],
              *, material_overlap: bool):
    """Fixture LLM. Oracle follows gold; adversarial always over-claims overlap."""
    if profile not in {"oracle", "adversarial_partial"}:
        raise ProtocolError(f"未知 llm_profile: {profile}")
    primary = paper_ids[0] if paper_ids else None
    if profile == "adversarial_partial":
        compare_matches = {a: "match" for a in AXES}
    elif material_overlap:
        compare_matches = {a: "match" for a in AXES}
    else:
        compare_matches = {a: "differ" for a in AXES}

    def llm(prompt, payload):
        if prompt.startswith("Decompose"):
            return {"axes": {a: a for a in AXES}, "queries": ["q1", "q2", "q3"]}
        if prompt.startswith("Triage"):
            score = 4 if (profile == "adversarial_partial" or material_overlap) else 2
            items = []
            for pid in paper_ids:
                items.append({
                    "paper_id": pid,
                    "overlap_score": score,
                    **{a: "triage" for a in AXES},
                })
            return {"papers": items}
        if prompt.startswith("Verify"):
            pid = payload.get("paper", {}).get("paper_id") or primary
            content = contents.get(pid, "")
            return {
                "paper_id": pid,
                "axis_matches": {a: "partial" for a in AXES},
                "assumptions_scope": "fixture",
                "closest_passage": _bound_passage(content),
            }
        if prompt.startswith("Compare"):
            return {
                "comparisons": [{"paper_id": pid, "axis_matches": dict(compare_matches)}
                                for pid in paper_ids],
                "closest_paper_id": primary,
            }
        if prompt.startswith("Write one concrete"):
            return {"delta": "Fixture delta sentence distinguishing candidate from prior."}
        raise ProtocolError(f"未覆盖的 Scoop LLM prompt: {prompt[:48]}")

    return llm


def _make_fetch(contents: dict[str, str]):
    def fetch(url, out_dir, paper_id):
        content = contents.get(paper_id)
        if not isinstance(content, str) or not content.strip():
            raise ProtocolError(f"fixture 缺少全文: {paper_id}")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        text_path = out / f"{paper_id}.txt"
        text_path.write_text(content, encoding="utf-8")
        return {
            "pdf": str(out / f"{paper_id}.pdf"),
            "pdf_sha256": "f" * 64,
            "text": str(text_path),
            "text_sha256": "e" * 64,
            "content": content,
            "source_url": url or "https://fixture.example/paper.pdf",
        }

    return fetch


def run_case(case_dir, *, llm_profile="oracle", work_dir=None):
    """Execute one fixture case; return per-case metrics dict."""
    case = _load_case(case_dir)
    papers = [dict(p) for p in case["papers"]]
    for paper in papers:
        paper["paper_id"] = _paper_id(paper)
    paper_ids = [p["paper_id"] for p in papers]
    contents = {}
    for paper in papers:
        pid = paper["paper_id"]
        # Prefer id key; fall back to doi / single fulltext.txt stem aliases.
        text = (case["fulltexts"].get(pid)
                or case["fulltexts"].get(str(paper.get("doi") or ""))
                or case["fulltexts"].get("paper"))
        if text is None and len(case["fulltexts"]) == 1:
            text = next(iter(case["fulltexts"].values()))
        if text is None:
            raise ProtocolError(f"找不到全文: {pid} in {case['dir']}")
        contents[pid] = text

    owned = work_dir is None
    root = Path(tempfile.mkdtemp(prefix="scoop-fpr-")) if owned else Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    try:
        idea = root / "idea" / "phase3_revise"
        idea.mkdir(parents=True)
        write_json(idea / "final_candidate.json", case["candidate"])
        scoop = root / "scoop"
        search = lambda *_args: {"papers": [{k: v for k, v in p.items() if k != "paper_id"}
                                            for p in papers]}
        run = ScoopRun(
            root / "idea", scoop, search,
            _make_llm(llm_profile, paper_ids, contents,
                      material_overlap=case["gold"]["material_overlap"]),
            _make_fetch(contents),
        )
        result = run.run(2020, 2026)
        report = result.get("report") or {}
        status = report.get("status") or result.get("phase")
        level = report.get("level")
        material = case["gold"]["material_overlap"]
        false_positive = (
            material is False
            and status == "completed"
            and type(level) is int
            and level <= LEVEL_FALSE_POSITIVE_MAX
        )
        return {
            "case_id": case["id"],
            "llm_profile": llm_profile,
            "material_overlap": material,
            "status": status,
            "level": level,
            "label": report.get("label"),
            "false_positive": false_positive,
            "work_dir": str(root),
        }
    finally:
        if owned:
            shutil.rmtree(root, ignore_errors=True)


def run(corpus_dir, out_dir=None):
    """Run manifest cases; write scoop_fpr_report.json under out_dir or corpus_dir."""
    corpus_dir = Path(corpus_dir).resolve()
    manifest_path = corpus_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ProtocolError("缺少 manifest.json")
    manifest = read_json(manifest_path)
    entries = manifest.get("cases")
    if not isinstance(entries, list) or not entries:
        raise ProtocolError("manifest.cases 必须是非空数组")
    out_dir = Path(out_dir).resolve() if out_dir else corpus_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ProtocolError("manifest case 需要字符串 id")
        case_id = entry["id"]
        profile = entry.get("llm_profile", "oracle")
        source = entry.get("case_dir", case_id)
        case_path = corpus_dir / source
        results.append(run_case(case_path, llm_profile=profile,
                                work_dir=out_dir / "_runs" / f"{case_id}-{profile}"))

    def _fpr(rows):
        neg = [r for r in rows if r["material_overlap"] is False]
        if not neg:
            return None, 0, 0
        fps = [r for r in neg if r["false_positive"]]
        return len(fps) / len(neg), len(neg), len(fps)

    oracle_fpr, n_oracle_neg, n_oracle_fp = _fpr(
        [r for r in results if r["llm_profile"] == "oracle"])
    adv_fpr, n_adv_neg, n_adv_fp = _fpr(
        [r for r in results if r["llm_profile"] == "adversarial_partial"])
    profiles = sorted({r["llm_profile"] for r in results})
    kind = ("controlled_mixed_fixture" if len(profiles) > 1
            else ("controlled_adversarial_fixture" if "adversarial_partial" in profiles
                  else "controlled_oracle_fixture"))
    report = {
        "schema": "scoop-fpr-1.0",
        "measurement_kind": kind,
        "controlled_scoop_overlap_fpr": None if oracle_fpr is None else round(oracle_fpr, 3),
        "adversarial_scoop_overlap_fpr": None if adv_fpr is None else round(adv_fpr, 3),
        "n_negative_oracle": n_oracle_neg,
        "n_false_positive_oracle": n_oracle_fp,
        "n_negative_adversarial": n_adv_neg,
        "n_false_positive_adversarial": n_adv_fp,
        "n_positive_sanity": sum(1 for r in results if r["material_overlap"] is True),
        "false_positive_rule": "completed and step7.level <= 3 on gold.material_overlap=false",
        "real_novelty_fpr": None,
        "pass": (oracle_fpr is not None and oracle_fpr <= 0.1),
        "pass_note": ("pass 仅看 oracle 负例 plumbing；adversarial_scoop_overlap_fpr 只作区分度探针，"
                      "不是产品验收；均非审计级 Scoop FPR"),
        "cases": results,
        "method": "离线 ScoopRun + fixture search/llm/fetch；与 A3 RejectionReview FPR 分离",
    }
    write_json(out_dir / "scoop_fpr_report.json", report)
    return report
