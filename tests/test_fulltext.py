import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from popper.core import ProtocolError
from popper.fulltext import PdfLinks, fetch_paper_text, pdf_candidates, public_https
from popper.scoop import AXES, ScoopRun, _paper_id


class ResolverTests(unittest.TestCase):
    def test_doi_pdf_without_extension_is_detected_by_content(self):
        paper = {"doi": "10.1234/test", "url": "https://doi.org/10.1234/test"}
        with patch("popper.fulltext.download", return_value=("https://journal.org/fileserve?id=3", b"%PDF-1.7\n")), \
                patch("popper.fulltext.extract_pdf", return_value={"content": "real text"}) as extract:
            result = fetch_paper_text(paper, Path("unused"), "p1")
        self.assertEqual("real text", result["content"])
        self.assertEqual("https://journal.org/fileserve?id=3", extract.call_args.args[-1])

    def test_landing_page_citation_pdf_url_is_followed(self):
        html = b'<meta name="citation_pdf_url" content="../download/7/9">'
        responses = [("https://journal.org/article/view/7", html),
                     ("https://journal.org/article/download/7/9", b"%PDF-1.7")]
        with patch("popper.fulltext.download", side_effect=responses) as download, \
                patch("popper.fulltext.extract_pdf", return_value={"content": "text"}):
            fetch_paper_text({"url": "https://journal.org/article/view/7"}, Path("unused"), "p1")
        self.assertEqual("https://journal.org/article/download/7/9", download.call_args.args[0])

    def test_oa_metadata_and_arxiv_urls_are_candidates(self):
        urls = pdf_candidates({"openAccessPdf": {"url": "https://journal.org/download/1"},
                               "url": "http://arxiv.org/abs/2501.12345v2"})
        self.assertEqual(["https://journal.org/download/1", "https://arxiv.org/pdf/2501.12345v2"], urls)

    def test_html_and_access_denial_remain_unavailable(self):
        for result in [("https://journal.org/paywall", b"<html>Abstract only</html>"),
                       urllib.error.HTTPError("https://journal.org/paper", 403, "denied", {}, None)]:
            side_effect = result if isinstance(result, Exception) else None
            with patch("popper.fulltext.download", return_value=result, side_effect=side_effect), \
                    patch("popper.fulltext.extract_pdf") as extract:
                with self.assertRaises(ProtocolError) as ctx:
                    fetch_paper_text({"url": "https://journal.org/paper"}, Path("unused"), "p1")
                self.assertTrue(ctx.exception.attempts)
                extract.assert_not_called()
        self.assertEqual(403, ctx.exception.attempts[0]["http_status"])

    def test_private_or_credentialed_urls_are_not_followed(self):
        for url in ("https://127.0.0.1/a", "https://localhost/a", "https://10.0.0.1/a",
                    "https://user:secret@journal.org/a", "file:///tmp/a.pdf"):
            self.assertFalse(public_https(url))
        parser = PdfLinks("https://journal.org/article")
        parser.feed('<meta name="citation_pdf_url" content="https://127.0.0.1/secret">')
        self.assertEqual([], parser.links)

    def test_crossref_similarity_checking_link_is_not_used(self):
        metadata = {"message": {"link": [{"URL": "https://staging.journal.org/private.pdf",
                    "content-type": "application/pdf", "intended-application": "similarity-checking"}]}}
        with patch("popper.fulltext.download", side_effect=[
                ("https://journal.org/landing", b"<html>abstract</html>"),
                ("https://api.crossref.org/works/10.1%2Fx", json.dumps(metadata).encode())]) as download:
            with self.assertRaises(ProtocolError):
                fetch_paper_text({"doi": "10.1/x"}, Path("unused"), "p1")
        self.assertEqual(2, download.call_count)


class FulltextEvidenceTests(unittest.TestCase):
    def make_run(self, root, fetch, fake_passage=False):
        idea = root / "idea"
        (idea / "phase3_revise").mkdir(parents=True)
        (idea / "phase3_revise" / "final_candidate.json").write_text(json.dumps({
            "title": "candidate", "core_mechanism": "mechanism", "falsification_prediction": "accuracy"}))
        paper = {"doi": "10.1/x", "url": "https://journal.org/a.pdf", "title": "paper"}
        pid = _paper_id(paper)
        def llm(prompt, payload):
            if prompt.startswith("Decompose"):
                return {"axes": {a: a for a in AXES}, "queries": ["a", "b", "c"]}
            if prompt.startswith("Triage"):
                return {"papers": [{"paper_id": pid, "overlap_score": 3, **{a: "match" for a in AXES}}]}
            if prompt.startswith("Verify"):
                return {"paper_id": pid, "axis_matches": {a: "partial" for a in AXES},
                        "closest_passage": "fabricated passage that does not appear" if fake_passage else
                                           "This is the actual methodological passage.",
                        "artifacts": {"text_sha256": "fake override"}}
            if prompt.startswith("Compare"):
                return {"comparisons": [{"paper_id": pid, "axis_matches": {a: "partial" for a in AXES}}],
                        "closest_paper_id": pid}
            return {"delta": "bounded difference"}
        return ScoopRun(idea, root / "scoop", lambda *args: {"papers": [paper]}, llm, fetch)

    @staticmethod
    def fetched(*args):
        return {"pdf": "original.pdf", "text": "original.txt", "text_sha256": "a" * 64,
                "pdf_sha256": "b" * 64, "content": "This is the actual methodological passage."}

    def test_model_cannot_replace_artifact_or_invent_evidence(self):
        for fake in (False, True):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                run = self.make_run(root, self.fetched, fake)
                result = run.run(2024, 2026)
                record = json.loads((root / "scoop" / "step5.json").read_text(encoding="utf-8"))["papers"][0]
                self.assertEqual("provisional" if fake else "completed", result["phase"])
                if not fake:
                    self.assertEqual("a" * 64, record["artifacts"]["text_sha256"])

    def test_comparisons_must_cover_selected_papers_and_valid_closest(self):
        from popper.scoop import _validate_comparisons
        item = {"paper_id": "p1", "axis_matches": {a: "partial" for a in AXES}}
        _validate_comparisons({"comparisons": [item], "closest_paper_id": "p1"}, ["p1"])
        for value in [
                {"comparisons": [], "closest_paper_id": None},
                {"comparisons": [item, item], "closest_paper_id": "p1"},
                {"comparisons": [item], "closest_paper_id": "invented"}]:
            with self.assertRaises(ProtocolError):
                _validate_comparisons(value, ["p1"])

    def test_verification_repairs_nonliteral_quote_once(self):
        from popper.scoop import _verify_fulltext
        literal = "This is the actual methodological passage."
        good = {"paper_id": "p1", "axis_matches": {a: "partial" for a in AXES},
                "closest_passage": literal}
        llm = Mock(side_effect=[{**good, "closest_passage": "A fabricated unsupported quotation."}, good])
        result = _verify_fulltext(llm, {}, {"paper_id": "p1"}, literal)
        self.assertEqual(literal, result["closest_passage"])
        self.assertEqual(2, llm.call_count)
        self.assertIn("validation_error", llm.call_args.args[1])

    def test_campaign_refresh_flag_reaches_fulltext_runner(self):
        from popper.campaign_nodes import BUILTIN_NODES
        from popper.orchestrator import Orchestrator
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scoop").mkdir()
            (root / "scoop" / "step7.json").write_text('{"status":"completed"}')
            registry = Mock()
            registry.scoop_run.return_value = {"phase": "completed", "last_step": 7}
            orch = Orchestrator(root)
            orch.init("test refresh", [{"key": "scoop"}])
            result = orch.run(nodes=BUILTIN_NODES, config={"base_url": "b", "model": "m",
                                                         "mode": "trusted_local",
                                                         "_registry": registry, "refresh_fulltext": True})
            self.assertEqual("completed", result["status"])
            self.assertTrue(registry.scoop_run.call_args.kwargs["refresh_fulltext"])

    def test_refresh_preserves_old_verdict_and_retries_fulltext(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fetch = Mock(side_effect=[OSError("temporarily unavailable"), self.fetched()])
            run = self.make_run(root, fetch)
            self.assertEqual("provisional", run.run(2024, 2026)["phase"])
            self.assertEqual("provisional", run.run(2024, 2026)["phase"])
            self.assertEqual(1, fetch.call_count)
            self.assertEqual("completed", run.run(2024, 2026, refresh_fulltext=True)["phase"])
            history = list((root / "scoop" / "history").glob("*/step7.json"))
            self.assertEqual(1, len(history))
            self.assertEqual("provisional", json.loads(history[0].read_text(encoding="utf-8"))["status"])
            self.assertEqual(2, fetch.call_count)


if __name__ == "__main__":
    unittest.main()
