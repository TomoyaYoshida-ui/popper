"""Manuscript templates and a bounded, summary-level research export."""
from __future__ import annotations

import json
import hashlib
import os
import platform
import sqlite3
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .core import ProtocolError, file_hash, inside, portable_path, read_json


DISCLOSURES = {
    "nature": "本稿件部分内容由 AI 辅助生成。作者已对全部内容进行人工审校并对准确性、完整性负责。"
              "依《自然》及其旗下期刊政策，须在投稿时披露生成式 AI 的使用情况，请在本声明中如实说明所用工具、用途及人工复核范围。",
    "acm": "本稿件部分内容由 AI 辅助生成。作者对内容的准确性负责并已逐项人工核验。"
           "依 ACM 出版政策，须披露生成式 AI 工具的使用，并说明其在研究、写作或辅助审阅中的具体作用。",
    "ieee": "本稿件部分内容由 AI 辅助生成，作者已对生成内容进行人工审校并承担全部责任。"
            "依 IEEE 出版伦理与投稿政策，须如实披露生成式 AI 的使用方式、工具名称及人工核验情况。",
}


class Materializer:
    def __init__(self, project_dir):
        self.root = Path(project_dir).resolve()

    # -- 稿件模板 -----------------------------------------------------------
    def materialize(self, manuscript_json, out_dir, template="md", disclosure="nature"):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(manuscript_json, list):
            raise ProtocolError("单次仅处理一篇稿件，不支持批量生成")
        if not isinstance(manuscript_json, dict):
            raise ProtocolError("manuscript 必须是 JSON 对象")
        sections = manuscript_json.get("sections", [])
        if not isinstance(sections, list):
            raise ProtocolError("manuscript sections 必须是列表")
        if disclosure is not None and disclosure not in DISCLOSURES:
            raise ProtocolError("未知披露声明口径")
        title = str(manuscript_json.get("title") or "Untitled")
        abstract = str(manuscript_json.get("abstract") or "")
        if template in ("docx", "pdf"):
            blocks = self._blocks(title, abstract, sections,
                                  None if disclosure is None else DISCLOSURES[disclosure])
            name = f"manuscript.{template}"
            path = out_dir / name
            if template == "docx":
                from .docxgen import DocxRenderer
                DocxRenderer().render(blocks, path)
            else:
                from .pdfgen import PdfRenderer
                PdfRenderer().render(blocks, path)
            return {"status": "materialized", "manuscript": str(path), "template": template,
                    "disclosure": disclosure}
        if template == "md":
            body = self._render_markdown(title, abstract, sections)
        elif template == "tex":
            body = self._render_tex(title, abstract, sections)
        else:
            raise ProtocolError("未知稿件模板")
        disclosure_text = self._render_disclosure(disclosure, template)
        if disclosure_text:
            if template == "md":
                body += "\n\n" + disclosure_text
            else:
                body = body.replace(r"\end{document}", disclosure_text + "\n" + r"\end{document}")
        name = f"manuscript.{template}"
        path = out_dir / name
        path.write_text(body, encoding="utf-8")
        return {"status": "materialized", "manuscript": str(path), "template": template,
                "disclosure": disclosure}

    @staticmethod
    def _render_disclosure(disclosure, template):
        if disclosure is None:
            return ""
        text = DISCLOSURES[disclosure]
        if template == "md":
            return "## AI 使用披露\n\n" + text
        return r"\section*{AI 使用披露}" + "\n" + text

    @staticmethod
    def _sections(sections):
        for index, item in enumerate(sections, 1):
            if isinstance(item, dict):
                heading = str(item.get("heading") or f"Section {index}")
                body = str(item.get("body") or "")
            else:
                heading = f"Section {index}"
                body = str(item or "")
            yield heading, body

    @staticmethod
    def _blocks(title, abstract, sections, disclosure_text):
        """构造 docx/pdf 可消费的结构化块列表，与 markdown 模板保持一致。"""
        blocks = [("title", title),
                  ("note", "物化层由已验证的科研 artifact 派生；以下为可交付稿件模板，不伪造内容。"),
                  ("label", "Abstract"), ("body", abstract)]
        for heading, body in Materializer._sections(sections):
            blocks.extend([("section", heading), ("body", body)])
        if disclosure_text:
            blocks.extend([("label", "AI 使用披露"), ("body", disclosure_text)])
        return blocks

    def _render_markdown(self, title, abstract, sections):
        lines = [f"# {title}", "", "> 物化层由已验证的科研 artifact 派生；以下为可交付稿件模板，不伪造内容。",
                 "", "## Abstract", "", abstract, ""]
        for heading, body in self._sections(sections):
            lines.extend([f"## {heading}", "", body, ""])
        return "\n".join(lines)

    def _render_tex(self, title, abstract, sections):
        lines = [r"\documentclass{article}", r"\usepackage[utf8]{inputenc}",
                 rf"\title{{{title}}}", r"\author{Popper 物化层生成骨架}",
                 r"\begin{document}", r"\maketitle", r"\begin{abstract}", abstract,
                 r"\end{abstract}"]
        for heading, body in self._sections(sections):
            lines.extend([rf"\section{{{heading}}}", body])
        lines.append(r"\end{document}")
        return "\n".join(lines)

    # -- 复现包 -------------------------------------------------------------
    def build_repro_package(self, out_dir, include_raw_evidence=False):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Build and self-verify in a private staging directory. A failed raw
        # replay must not replace a previously valid export with a broken ZIP.
        with tempfile.TemporaryDirectory(prefix="popper-export-", dir=out_dir) as staged:
            result = self._build_repro_package_in_dir(staged, include_raw_evidence)
            final = out_dir / "reproducibility.zip"
            os.replace(result["path"], final)
            result["path"] = str(final)
            return result

    def _build_repro_package_in_dir(self, out_dir, include_raw_evidence):
        if not (self.root / "experiment.json").is_file():
            raise ProtocolError("复现包必须含 experiment.json；项目目录缺少受控实验配置")
        spec = read_json(self.root / "experiment.json")
        if not isinstance(spec, dict):
            raise ProtocolError("experiment.json 必须包含规范的实验配置")
        code_files = spec.get("code_files")
        if not isinstance(code_files, list):
            raise ProtocolError("experiment.json 缺少 code_files")
        declared = ["experiment.json", *code_files, spec.get("train"), spec.get("dev"),
                    spec.get("test")]
        if spec.get("provenance_files"):
            declared.extend(spec["provenance_files"])
        files = []
        for name in declared:
            # 越界检查必须走 inside()：它对拼接结果先 resolve() 再判前缀。原先写成
            # `(self.root / name).is_relative_to(self.root)`，而 resolve() 之前的 `..`
            # 不折叠（`C:\proj\..\secret\x.json` 仍以前缀 `C:\proj` 开头），守卫对任何
            # `../` 都恒真——形同虚设，项目外的文件会被打进 reproducibility.zip。
            inside(self.root, name)
            # 收进 files 的是归一值而非原始串：arcname 是压缩包内的条目名，与宿主分隔符
            # 无关。这里的 experiment.json 是项目自带输入，不经过 validate_spec，`init`
            # 也刻意不回写它，盘上可能仍是 `pkg\model.py` 这种写法；拿原始串当 arcname
            # 会让同一份包在两侧长出不同条目名（Linux 上就是一个含反斜杠的怪名字）。
            # 只归一产物，盘上的 experiment.json 保持用户原样。
            files.append(portable_path(name))
        if len(set(files)) != len(files):
            raise ProtocolError("复现包登记了重复输入路径")
        reserved = {"environment.lock", "results.json", "verify_claims.py",
                    "run.sh", "README_REPRO.md", "manifest.json"}
        reserved_folded = {item.casefold() for item in reserved}
        for name in files:
            folded = name.casefold()
            if folded in reserved_folded or folded.startswith(".popper/"):
                raise ProtocolError(f"复现包输入占用保留成员名: {name}")
        if len({name.casefold() for name in files}) != len(files):
            raise ProtocolError("复现包登记了大小写冲突的输入路径")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        zip_path = out_dir / "reproducibility.zip"
        lock_present = (self.root / "environment.lock").is_file()
        # Only confirmed test summaries are exportable here. This is not raw
        # prediction replay and must never be described as independent scoring.
        aggregated = self._aggregate_results()
        with tempfile.TemporaryDirectory(prefix="popper-repro-stage-") as temporary:
            raw_files = (self._portable_evidence(Path(temporary))
                         if include_raw_evidence else [])
            planned = [*files, "environment.lock", "run.sh", "README_REPRO.md",
                       "manifest.json"]
            if aggregated is not None:
                planned.extend(("results.json", "verify_claims.py"))
            planned.extend(name for name, _ in raw_files)
            if len({name.casefold() for name in planned}) != len(planned):
                raise ProtocolError("复现包成员名重复或大小写冲突")
            with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
                for name in files:
                    archive.write(self.root / name, arcname=name)
                if lock_present:
                    archive.write(self.root / "environment.lock", arcname="environment.lock")
                    files.append("environment.lock")
                else:
                    lock = {"python": sys.version, "platform": platform.platform(),
                            "_generated_stub": True,
                            "note": "未找到环境锁；此为占位，未锁定真实依赖闭包。验证器已标注 generated_stub。"}
                    archive.writestr("environment.lock",
                                     json.dumps(lock, ensure_ascii=False, indent=2))
                    files.append("environment.lock")
                if aggregated is not None:
                    archive.writestr("results.json", json.dumps(aggregated, ensure_ascii=False, indent=2))
                    files.append("results.json")
                    archive.writestr("verify_claims.py", self._verify_script(spec))
                    files.append("verify_claims.py")
                for name, path in raw_files:
                    archive.write(path, arcname=name)
                    files.append(name)
                archive.writestr("run.sh", RUN_SCRIPT)
                archive.writestr("README_REPRO.md", self._readme(
                    lock_present, aggregated is not None, include_raw_evidence))
                files.extend(["run.sh", "README_REPRO.md"])
        # Hash the bytes actually written into the archive, not a second read
        # of mutable project files after packaging (avoids a TOCTOU manifest).
        with ZipFile(zip_path, "a", ZIP_DEFLATED) as archive:
            digests = {}
            for name in files:
                hasher = hashlib.sha256()
                with archive.open(name) as member:
                    for chunk in iter(lambda: member.read(1024 * 1024), b""):
                        hasher.update(chunk)
                digests[name] = hasher.hexdigest()
            level = ("raw_prediction_replay_with_installed_popper" if include_raw_evidence
                     else "summary_consistency_only")
            archive.writestr("manifest.json", json.dumps({
                "schema_version": "1.0", "verification_level": level,
                "signed": False, "files": digests,
            }, ensure_ascii=False, sort_keys=True, indent=2))
            files.append("manifest.json")
        if include_raw_evidence:
            # A package is not called portable merely because it contains a DB:
            # prove an independent extraction can recompute the saved claim.
            with tempfile.TemporaryDirectory(prefix="popper-repro-verify-") as temporary:
                with ZipFile(zip_path) as archive:
                    archive.extractall(temporary)
                from .core import Experiment
                reproduced = Experiment(temporary)
                try:
                    replay = reproduced.replay(portable=True)
                finally:
                    reproduced.close()
                if not replay["claim_recomputed"]:
                    raise ProtocolError("便携复现包未能从原始预测重算已确认 claim")
        with ZipFile(zip_path) as check:
            if "experiment.json" not in check.namelist():
                raise ProtocolError("复现包校验失败：缺少 experiment.json")
        archive_hash = hashlib.sha256()
        with zip_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                archive_hash.update(chunk)
        return {"status": "built", "path": str(zip_path), "files": files,
                "sha256": archive_hash.hexdigest(),
                "verification_level": level}

    def _portable_evidence(self, staging: Path):
        """Snapshot the authority DB and only event-declared run artifacts."""
        from .core import Experiment
        experiment = Experiment(self.root)
        try:
            state = experiment.verify_inputs()
            if state["phase"] != "completed" or not state.get("claim"):
                raise ProtocolError("原始证据导出要求已完成确认的实验")
            replay = experiment.replay()
            if not replay["claim_recomputed"]:
                raise ProtocolError("原始证据未形成可重算 claim")
            snapshot = staging / "state.db"
            destination = sqlite3.connect(snapshot)
            try:
                experiment.db.backup(destination)
            finally:
                destination.close()
            names = [(".popper/state.db", snapshot)]
            seen = {".popper/state.db"}
            for row in experiment.db.execute(
                    "SELECT payload FROM events WHERE kind='run_completed' ORDER BY seq"):
                event = json.loads(row[0])
                run_id = event["run_id"]
                run_root = (experiment.home / "runs").resolve()
                run_dir = (run_root / run_id).resolve()
                if not run_dir.is_relative_to(run_root) or not run_dir.is_dir():
                    raise ProtocolError(f"运行目录缺失或越界: {run_id}")
                for name, expected in event["artifacts"].items():
                    path = inside(run_dir, name)
                    if file_hash(path) != expected:
                        raise ProtocolError(f"运行工件哈希不匹配: {run_id}/{name}")
                    member = ".popper/runs/" + portable_path(run_id) + "/" + portable_path(name)
                    if member not in seen:
                        names.append((member, path))
                        seen.add(member)
            return names
        finally:
            experiment.close()

    def _aggregate_results(self):
        """从已确认实验的 .popper 状态聚合 test 摘要；未完成则返回 None。

        仅读取已保存的真实运行结果，不反写、不伪造。附带冻结候选 selected。
        """
        state_db = self.root / ".popper" / "state.db"
        if not state_db.is_file():
            return None
        try:
            from .core import Experiment
            exp = Experiment(self.root)
            try:
                state = exp.state()
                if state.get("phase") != "completed" or not state.get("claim"):
                    return None
                results = exp.results("test")
                selected = state.get("selected")
                claim = state["claim"]
            finally:
                exp.close()
            # 只导出 test 摘要、冻结候选及已保存结论；不伪装逐样本 replay。
            return {"schema_version": "1.0", "selected": selected,
                    "claim": {"delta": claim["delta"], "status": claim["status"]},
                    "runs": [
                {"config": r["config"], "split": r["split"], "mean": r["mean"]}
                for r in results]}
        except Exception as error:
            raise ProtocolError(
                f"已登记实验状态不可读取，拒绝生成可能误导的复现包: {type(error).__name__}") from error

    def _verify_script(self, spec):
        metric = spec["metric"]["name"]
        direction = spec["metric"]["direction"]
        threshold = spec["min_improvement"]
        metric_lit = json.dumps(metric, ensure_ascii=False)
        direction_lit = json.dumps(direction, ensure_ascii=False)
        threshold_lit = json.dumps(threshold, ensure_ascii=False)
        return "\n".join([
            "#!/usr/bin/env python3",
            "'''Check archive hashes and confirmed test-summary delta without Popper.",
            "This cannot independently recompute metrics from raw predictions or verify a scientific claim.'''",
            "from __future__ import annotations",
            "import hashlib, json, math, pathlib, sys",
            "root = pathlib.Path(__file__).resolve().parent",
            "if hasattr(sys.stdout, 'reconfigure'):",
            "    sys.stdout.reconfigure(encoding='utf-8', errors='replace')",
            "manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))",
            "for name, expected in manifest['files'].items():",
            "    path = (root / name).resolve()",
            "    if not path.is_relative_to(root) or not path.is_file():",
            "        print(json.dumps({'verified': False, 'reason': 'missing_or_unsafe_member', 'path': name}))",
            "        raise SystemExit(3)",
            "    actual = hashlib.sha256(path.read_bytes()).hexdigest()",
            "    if actual != expected:",
            "        print(json.dumps({'verified': False, 'reason': 'member_hash_mismatch', 'path': name}))",
            "        raise SystemExit(3)",
            "spec = json.loads((root / 'experiment.json').read_text(encoding='utf-8'))",
            "results = json.loads((root / 'results.json').read_text(encoding='utf-8'))",
            "runs = results.get('runs', [])",
            f"metric = {metric_lit}",
            f"direction = {direction_lit}",
            f"threshold = {threshold_lit}",
            "selected_config = results.get('selected')",
            "test_runs = [r for r in runs if r.get('split') == 'test']",
            "baseline = [r for r in test_runs if r.get('config') == spec['baseline']]",
            "selected = [r for r in test_runs if selected_config is not None",
            "            and r.get('config') == selected_config and r.get('config') != spec['baseline']]",
            "out = {'metric': metric, 'verified': False,",
            "       'verification_level': 'summary_consistency_only',",
            "       'scientific_claim_verified': False,",
            "       'claim_status_recorded': results.get('claim', {}).get('status')}",
            "if len(baseline) != 1 or len(selected) != 1 or len(test_runs) != 2:",
            "    out['reason'] = 'missing_or_ambiguous_confirmed_test_runs'",
            "elif any(type(r.get('mean')) not in (int, float) or not math.isfinite(r['mean'])",
            "         for r in (baseline[0], selected[0])):",
            "    out['reason'] = 'non_finite_summary'",
            "else:",
            "    delta = (baseline[0]['mean'] - selected[0]['mean'] if direction == 'min'",
            "             else selected[0]['mean'] - baseline[0]['mean'])",
            "    recorded_delta = results.get('claim', {}).get('delta')",
            "    out['delta'] = delta",
            "    out['threshold'] = threshold",
            "    out['selected_config'] = selected_config",
            "    out['supports_threshold_from_mean'] = bool(delta > 0 and delta >= threshold)",
            "    out['verified'] = (type(recorded_delta) in (int, float)",
            "                       and math.isfinite(recorded_delta)",
            "                       and math.isclose(delta, recorded_delta, rel_tol=0, abs_tol=1e-12))",
            "    if not out['verified']: out['reason'] = 'recorded_delta_mismatch'",
            "print(json.dumps(out, ensure_ascii=False))",
            "if not out['verified']:",
            "    raise SystemExit(2)",
            "",
        ])

    def _readme(self, lock_present, results_present=False, raw_evidence=False):
        lock_note = ("已在包内含 environment.lock，用于复现时锁定运行环境。"
                     if lock_present else
                     "未找到 environment.lock；包内为 generated_stub 占位，复现前请锁定宿主环境（Python 版本等）。")
        verify_note = ("包内含摘要一致性检查脚本 verify_claims.py 与 run.sh；它检查包内文件哈希及确认测试摘要的 delta，"
                       "不从逐样本原始证据独立计分，也不验证完整科学主张。"
                       if results_present else
                       "项目尚未完成确认或缺少已保存 claim；本包不生成 results.json/verify_claims.py，不能检查确认结论。")
        return "\n".join([
            "# Popper 复现包", "",
            "本包保存登记的代码、输入与环境信息；只有已完成确认的项目才附带确认摘要。",
            "", "## 环境锁定", "", lock_note, "",
            "## 包内检查", "", verify_note,
            "manifest.json 列出包内文件 SHA-256；它本身未签名，需从可信渠道独立取得整个 ZIP 的 SHA-256 才能防止清单与文件一起被替换。", "",
            "进入解压后的项目目录：", "    python verify_claims.py   # 仅已完成确认的项目；检查摘要一致性",
            "    # 或", "    bash run.sh                  # 同一检查入口",
            "",
            "## 从原始证据独立重算", "",
            ("本包含原始运行库及事件登记的预测/工件。安装同版本 Popper 后运行："
             if raw_evidence else
             "本包未包含 .popper 原始运行库；只能在原项目有该库时运行："),
            ("    python -m popper experiment replay . --portable"
             if raw_evidence else
             "    python -m popper experiment replay <原项目>"),
            "",
            "## 当前限制", "",
            ("本 ZIP 包含可能敏感的原始预测、运行日志和数据库；分享前须人工审查。"
             if raw_evidence else
             "本 ZIP 不包含 .popper 原始运行库与逐样本预测，因此不能独立完成 replay。"),
            "摘要一致性不等于模型重跑、独立计分、统计主张核验或机制证明。",
            "",
        ])


RUN_SCRIPT = "\n".join([
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    "# Popper 物化层摘要一致性检查（不从原始证据重算）。",
    "cd \"${1:-.}\"",
    "if [ -f results.json ] && [ -f verify_claims.py ]; then",
    "    echo \"==> 包内摘要一致性检查：python verify_claims.py\"",
    "    python verify_claims.py",
    "    echo \"==> 摘要一致；科学主张仍需原始证据 replay。\"",
    "    exit 0",
    "fi",
    "echo \"==> 本包未含确认摘要；请在原项目用 Popper replay 原始运行证据。\" >&2",
    "exit 2",
    "",
])
