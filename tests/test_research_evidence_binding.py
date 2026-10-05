"""A non-ML Claim can become prose only through checked evidence bindings."""
import tempfile
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from popper.cli import main
from popper.core import Experiment, ProtocolError, initialize, read_json, write_json
from popper.manuscript_flow import ManuscriptFlow
from popper.research.bound_manuscript import (_expected, _validate_input,
                                              materialize_bound_draft,
                                              verify_bound_draft)
from popper.research.controller import ResearchController
from popper.research.evidence_binding import (export_research_evidence,
                                              render_research_evidence,
                                              verify_research_evidence)
from popper.server import Workstation
from tests.test_cross_domain_kernel import EXAMPLES, copy_registered_project


class ResearchEvidenceBindingTests(unittest.TestCase):
    def test_structured_draft_must_include_every_claim_exactly_once(self):
        bindings = {"clm_a": {}, "clm_b": {}}
        spec = {"schema_version": "1.0", "title": "研究草稿",
                "sections": [{"heading": "结果", "blocks": [
                    {"kind": "claim", "claim_id": "clm_a"},
                    {"kind": "claim", "claim_id": "clm_b"}]}]}
        _validate_input(spec, bindings)
        spec["sections"][0]["blocks"].pop()
        with self.assertRaisesRegex(ProtocolError, "全部 Claim"):
            _validate_input(spec, bindings)
        spec["sections"][0]["blocks"].append(
            {"kind": "claim", "claim_id": "clm_a"})
        with self.assertRaisesRegex(ProtocolError, "全部 Claim"):
            _validate_input(spec, bindings)
        spec["sections"][0]["blocks"][1]["claim_id"] = ["clm_b"]
        with self.assertRaisesRegex(ProtocolError, "未验证的 Claim"):
            _validate_input(spec, bindings)

    def test_ode_claim_export_verification_and_tamper_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, run_dir, output = (root / "project", root / "research",
                                        root / "evidence")
            copy_registered_project(EXAMPLES / "ode-convergence", project)
            initialize(project)
            ResearchController.initialize(project, run_dir)
            controller = ResearchController(run_dir)
            try:
                with self.assertRaisesRegex(ProtocolError, "Study"):
                    export_research_evidence(project, run_dir, output)
                self.assertFalse(output.exists())
                controller.run(trusted_local=True, max_steps=2)
                final = controller.confirm(trusted_local=True)
                self.assertEqual(1, len(final["claims"]))
            finally:
                controller.close()

            result = export_research_evidence(project, run_dir, output)
            self.assertEqual(1, result["claim_count"])
            bundle = read_json(output / "research-evidence.json")
            self.assertEqual(bundle, render_research_evidence(project, run_dir)["bundle"])
            server = Workstation(("127.0.0.1", 0), project,
                                 research_runs=[run_dir], trusted_local=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                endpoint = base + f"/api/v1/research/{bundle['study_id']}/evidence-preview"
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(endpoint, timeout=5)
                self.assertEqual(403, denied.exception.code)
                with urllib.request.urlopen(base + "/api/session", timeout=5) as response:
                    token = json.load(response)["token"]
                request = urllib.request.Request(
                    endpoint, headers={"X-Popper-Token": token})
                with urllib.request.urlopen(request, timeout=10) as response:
                    preview = json.load(response)["data"]
                self.assertEqual(bundle, preview["bundle"])
                self.assertEqual((output / "research-evidence.json").read_text(encoding="utf-8"),
                                 preview["json_text"])
                self.assertEqual((output / "research-evidence.md").read_text(encoding="utf-8"),
                                 preview["markdown"])
                spec = {"schema_version": "1.0", "title": "数值收敛研究草稿",
                        "sections": [{"heading": "结果", "blocks": [
                            {"kind": "claim", "claim_id": bundle["bindings"][0]["claim_id"]},
                            {"kind": "author_note", "text": "方法解释有待人工审校。"}]}]}
                draft_url = base + f"/api/v1/research/{bundle['study_id']}/bound-drafts"
                body = json.dumps({"schema_version": "1.0", "spec": spec}).encode()
                unauthenticated = urllib.request.Request(
                    draft_url, data=body, headers={"Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(unauthenticated, timeout=5)
                self.assertEqual(403, denied.exception.code)
                request = urllib.request.Request(
                    draft_url, data=body,
                    headers={"X-Popper-Token": token, "Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    created = json.load(response)["data"]
                self.assertFalse(created["replayed"])
                self.assertEqual("draft_needs_review", created["status"])
                self.assertFalse(created["publication_attested"])
                self.assertEqual(created["manuscript"],
                                 Path(created["manuscript_path"]).read_text(encoding="utf-8"))
                with urllib.request.urlopen(request, timeout=10) as response:
                    replayed = json.load(response)["data"]
                self.assertTrue(replayed["replayed"])
                self.assertEqual(created["manuscript_path"], replayed["manuscript_path"])
                with urllib.request.urlopen(urllib.request.Request(
                        draft_url, headers={"X-Popper-Token": token}), timeout=10) as response:
                    listed = json.load(response)["data"]["drafts"]
                self.assertEqual(1, len(listed))
                self.assertTrue(listed[0]["ok"])
                self.assertEqual(created["draft_id"], listed[0]["draft_id"])
                detail_url = draft_url + "/" + created["draft_id"]
                detail_request = urllib.request.Request(
                    detail_url, headers={"X-Popper-Token": token})
                with urllib.request.urlopen(detail_request, timeout=10) as response:
                    detail = json.load(response)["data"]
                self.assertEqual("draft", detail["flow_state"])
                review_url = detail_url + "/review"
                review_body = json.dumps({"schema_version": "1.0",
                                          "reason": "已人工核对稿件与作者说明"}).encode()
                review_request = urllib.request.Request(
                    review_url, data=review_body,
                    headers={"X-Popper-Token": token,
                             "Content-Type": "application/json"})
                manuscript_path = Path(created["manuscript_path"])
                original_workbench_text = manuscript_path.read_text(encoding="utf-8")
                manuscript_path.write_text("未绑定的结果", encoding="utf-8")
                with self.assertRaises(urllib.error.HTTPError) as rejected_review:
                    urllib.request.urlopen(review_request, timeout=10)
                self.assertEqual(400, rejected_review.exception.code)
                manuscript_path.write_text(original_workbench_text, encoding="utf-8")
                with urllib.request.urlopen(review_request, timeout=10) as response:
                    reviewed = json.load(response)["data"]
                self.assertEqual("reviewed", reviewed["status"])
                with urllib.request.urlopen(detail_request, timeout=10) as response:
                    detail = json.load(response)["data"]
                self.assertEqual("reviewed", detail["flow_state"])
                with self.assertRaises(urllib.error.HTTPError) as duplicate_review:
                    urllib.request.urlopen(review_request, timeout=10)
                self.assertEqual(400, duplicate_review.exception.code)
                manuscript_path.write_text("被修改的草稿", encoding="utf-8")
                with self.assertRaises(urllib.error.HTTPError) as tampered:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(400, tampered.exception.code)
                self.assertEqual("被修改的草稿", manuscript_path.read_text(encoding="utf-8"))
                with urllib.request.urlopen(urllib.request.Request(
                        draft_url, headers={"X-Popper-Token": token}), timeout=10) as response:
                    listed = json.load(response)["data"]["drafts"]
                self.assertFalse(listed[0]["ok"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.assertEqual("local_research_store_and_source_artifacts",
                             bundle["verification_level"])
            binding = bundle["bindings"][0]
            self.assertEqual(final["claims"][0]["claim_id"], binding["claim_id"])
            self.assertEqual(final["claims"][0]["oriented_delta"],
                             binding["oriented_delta"])
            self.assertEqual(2, len(binding["sources"]))
            self.assertTrue(verify_research_evidence(project, run_dir, output)["ok"])
            self.assertEqual(0, main(["research", "materialize", "verify-evidence",
                                      str(project), str(run_dir), str(output)]))
            cli_output = root / "cli-evidence"
            self.assertEqual(0, main(["research", "materialize", "evidence",
                                      str(project), str(run_dir), str(cli_output)]))
            self.assertTrue(verify_research_evidence(project, run_dir, cli_output)["ok"])
            draft_spec = root / "draft-input.json"
            write_json(draft_spec, {
                "schema_version": "1.0", "title": "数值收敛研究草稿",
                "sections": [{"heading": "结果", "blocks": [
                    {"kind": "claim", "claim_id": binding["claim_id"]},
                    {"kind": "author_note", "text": "方法解释尚待人工审校。"}]}],
            })
            draft_dir = root / "bound-draft"
            self.assertEqual("draft_needs_review", materialize_bound_draft(
                project, run_dir, output, draft_spec, draft_dir)["status"])
            self.assertTrue(verify_bound_draft(
                project, run_dir, output, draft_spec, draft_dir)["ok"])
            draft_text = (draft_dir / "manuscript.md").read_text(encoding="utf-8")
            self.assertIn(binding["claim_id"], draft_text)
            self.assertIn("作者说明（未验证）", draft_text)
            self.assertIn("未经人工审校", draft_text)
            self.assertEqual(0, main([
                "research", "materialize", "verify-bound-draft",
                str(project), str(run_dir), str(output), str(draft_dir),
                "--manuscript-json", str(draft_spec)]))
            cli_draft = root / "cli-bound-draft"
            self.assertEqual(0, main([
                "research", "materialize", "bound-draft",
                str(project), str(run_dir), str(output), str(cli_draft),
                "--manuscript-json", str(draft_spec)]))
            self.assertTrue(verify_bound_draft(
                project, run_dir, output, draft_spec, cli_draft)["ok"])
            draft_file = draft_dir / "manuscript.md"
            original_draft = draft_file.read_text(encoding="utf-8")
            draft_file.write_text(original_draft + "\n未经绑定的结论", encoding="utf-8")
            with self.assertRaisesRegex(ProtocolError, "不一致"):
                verify_bound_draft(project, run_dir, output, draft_spec, draft_dir)
            draft_file.write_text(original_draft, encoding="utf-8")
            experiment = Experiment(project)
            try:
                flow = ManuscriptFlow(experiment)
                with self.assertRaisesRegex(ProtocolError, "review-bound"):
                    flow.transition(draft_file, "reviewed")
                flow.review_bound(draft_file, run_dir, output, draft_spec,
                                  "已人工审阅稿件")
                with self.assertRaisesRegex(ProtocolError, "submit-bound"):
                    flow.submit(draft_file)
                with self.assertRaisesRegex(ProtocolError, "submit-bound"):
                    flow.transition(draft_file, "submitted")
                binding_manifest = draft_dir / "binding-manifest.json"
                manifest_bytes = binding_manifest.read_bytes()
                binding_manifest.unlink()
                with self.assertRaisesRegex(ProtocolError, "submit-bound"):
                    flow.transition(draft_file, "submitted")
                binding_manifest.write_bytes(manifest_bytes)
                entry = flow._entry(draft_file)
                entry["state"] = "submitted"
                flow._save()
                with self.assertRaisesRegex(ProtocolError, "状态投影"):
                    flow.submit_bound(draft_file, run_dir, output, draft_spec)
                entry["state"] = "reviewed"
                flow._save()
                submitted = flow.submit_bound(draft_file, run_dir, output, draft_spec)
                self.assertEqual("submitted", submitted["status"])
                self.assertFalse(submitted["bound_verification"]["publication_attested"])
                with self.assertRaisesRegex(ProtocolError, "publish-bound"):
                    flow.publish(draft_file)
                draft_file.write_text(original_draft + "伪造段落", encoding="utf-8")
                with self.assertRaises(ProtocolError):
                    flow.publish_bound(draft_file, run_dir, output, draft_spec)
                draft_file.write_text(original_draft, encoding="utf-8")
                published = flow.publish_bound(draft_file, run_dir, output,
                                               draft_spec)
                self.assertEqual("published", published["status"])
                self.assertFalse(published["publication_attested"])
            finally:
                experiment.close()
            cli_file = cli_draft / "manuscript.md"
            self.assertEqual(0, main([
                "research", "paper", "review-bound", str(project), str(run_dir),
                str(output), "--manuscript", str(cli_file),
                "--draft-spec-json", str(draft_spec),
                "--reason", "已人工审阅 CLI 草稿"]))
            experiment = Experiment(project)
            try:
                flow = ManuscriptFlow(experiment)
                original_spec = draft_spec.read_bytes()
                original_cli_file = cli_file.read_bytes()
                cli_manifest_path = cli_draft / "binding-manifest.json"
                original_cli_manifest = cli_manifest_path.read_bytes()
                changed_spec = read_json(draft_spec)
                changed_spec["title"] = "数值收敛研究改写稿"
                write_json(draft_spec, changed_spec)
                changed_text, changed_manifest = _expected(
                    project, run_dir, output, draft_spec)
                cli_file.write_text(changed_text, encoding="utf-8")
                write_json(cli_manifest_path, changed_manifest)
                self.assertTrue(verify_bound_draft(
                    project, run_dir, output, draft_spec, cli_draft)["ok"])
                with self.assertRaisesRegex(ProtocolError, "审校时"):
                    flow.submit_bound(cli_file, run_dir, output, draft_spec)
                draft_spec.write_bytes(original_spec)
                cli_file.write_bytes(original_cli_file)
                cli_manifest_path.write_bytes(original_cli_manifest)
            finally:
                experiment.close()
            self.assertEqual(0, main([
                "research", "paper", "submit-bound", str(project), str(run_dir),
                str(output), "--manuscript", str(cli_draft / "manuscript.md"),
                "--draft-spec-json", str(draft_spec)]))
            self.assertEqual(0, main([
                "research", "paper", "publish-bound", str(project), str(run_dir),
                str(output), "--manuscript", str(cli_draft / "manuscript.md"),
                "--draft-spec-json", str(draft_spec)]))
            forged_draft = read_json(draft_spec)
            forged_draft["sections"][0]["blocks"][1]["text"] = "效果改善 99%。"
            write_json(draft_spec, forged_draft)
            with self.assertRaisesRegex(ProtocolError, "裸数字"):
                materialize_bound_draft(project, run_dir, output, draft_spec,
                                        root / "forged-draft")
            with self.assertRaises(ProtocolError):
                verify_bound_draft(project, run_dir, output, draft_spec, draft_dir)
            forged_draft["sections"][0]["blocks"][1]["text"] = "方法解释尚待人工审校。"
            write_json(draft_spec, forged_draft)
            self.assertTrue(verify_bound_draft(
                project, run_dir, output, draft_spec, draft_dir)["ok"])
            incomplete = read_json(draft_spec)
            incomplete["sections"][0]["blocks"] = [
                {"kind": "author_note", "text": "未验证说明。"}]
            write_json(root / "missing-claim.json", incomplete)
            with self.assertRaisesRegex(ProtocolError, "全部 Claim"):
                materialize_bound_draft(project, run_dir, output,
                                        root / "missing-claim.json",
                                        root / "missing-output")
            with self.assertRaisesRegex(ProtocolError, "已存在"):
                export_research_evidence(project, run_dir, output)
            with self.assertRaisesRegex(ProtocolError, "当前项目"):
                verify_research_evidence(root, run_dir, output)

            modified = read_json(output / "research-evidence.json")
            modified["bindings"][0]["oriented_delta"] += 1
            write_json(output / "research-evidence.json", modified)
            with self.assertRaisesRegex(ProtocolError, "不一致"):
                verify_research_evidence(project, run_dir, output)
            write_json(output / "research-evidence.json", bundle)
            markdown = output / "research-evidence.md"
            original_markdown = markdown.read_text(encoding="utf-8")
            markdown.write_text(original_markdown + "伪造结论", encoding="utf-8")
            with self.assertRaisesRegex(ProtocolError, "不一致"):
                verify_research_evidence(project, run_dir, output)
            markdown.write_text(original_markdown, encoding="utf-8")
            artifact = next(row["artifact_id"] for row in final["observations"]
                            if row["observation_id"] == binding["sources"][0]["observation_id"])
            artifact_path = Path(artifact)
            original_artifact = artifact_path.read_bytes()
            artifact_path.write_bytes(original_artifact + b" ")
            with self.assertRaisesRegex(ProtocolError, "完整性"):
                verify_research_evidence(project, run_dir, output)
            artifact_path.write_bytes(original_artifact)
            manifest_path = run_dir / "research.json"
            manifest = read_json(manifest_path)
            manifest["policy"] = "forged"
            write_json(manifest_path, manifest)
            with self.assertRaisesRegex(ProtocolError, "冻结登记"):
                verify_research_evidence(project, run_dir, output)
