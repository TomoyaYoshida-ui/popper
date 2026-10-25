"""Controlled Scoop overlap FPR harness. Offline; no network."""
from pathlib import Path

from popper.research.scoop_context import load_scoop_hits
from popper.scoop_measure import run, run_case

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "scoop-controlled"


def test_oracle_negative_is_not_false_positive(tmp_path):
    result = run_case(FIXTURES / "neg-near-miss-a", llm_profile="oracle",
                      work_dir=tmp_path / "neg-a")
    assert result["status"] == "completed"
    assert result["level"] == 5
    assert result["false_positive"] is False


def test_oracle_positive_sanity_marks_strong_overlap(tmp_path):
    result = run_case(FIXTURES / "pos-clear-overlap", llm_profile="oracle",
                      work_dir=tmp_path / "pos")
    assert result["status"] == "completed"
    assert result["level"] <= 2
    assert result["material_overlap"] is True
    assert result["false_positive"] is False


def test_adversarial_negative_is_false_positive(tmp_path):
    result = run_case(FIXTURES / "neg-near-miss-a", llm_profile="adversarial_partial",
                      work_dir=tmp_path / "adv")
    assert result["status"] == "completed"
    assert result["level"] <= 3
    assert result["false_positive"] is True


def test_false_positive_completed_run_yields_citable_scoop_hits(tmp_path):
    """Slice A linkage: completed+fulltext FP runs still project SCOOP-* ids."""
    work = tmp_path / "fp-hits"
    result = run_case(FIXTURES / "neg-near-miss-a", llm_profile="adversarial_partial",
                      work_dir=work)
    assert result["false_positive"] is True
    hits = load_scoop_hits(work)
    assert len(hits) == 1
    assert hits[0]["id"].startswith("SCOOP-")
    assert hits[0].get("verified_fulltext") is True


def test_manifest_report_separates_oracle_and_adversarial(tmp_path):
    report = run(FIXTURES, out_dir=tmp_path / "out")
    assert report["real_novelty_fpr"] is None
    assert report["controlled_scoop_overlap_fpr"] == 0.0
    assert report["pass"] is True
    assert report["adversarial_scoop_overlap_fpr"] == 1.0
    assert (tmp_path / "out" / "scoop_fpr_report.json").is_file()
    assert report["n_negative_oracle"] == 2
    assert report["n_negative_adversarial"] == 1
