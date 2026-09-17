import os
import shutil
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from popper.core import ProtocolError
from popper.materialize import Materializer


def _make_project(root, include_experiment=True):
    (root / "model.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    for name in ("train.json", "dev.json", "test.json"):
        write_rows(root, name)
    if include_experiment:
        spec = {
            "name": "物化层冒烟实验", "objective": "验证素材化",
            "entrypoint": "model.py", "code_files": ["model.py"],
            "train": "train.json", "dev": "dev.json", "test": "test.json",
            "baseline": {"degree": 1}, "candidates": [{"degree": 2}],
            "metric": {"name": "mse", "direction": "min"}, "seeds": [11],
            "budget": 1, "timeout_seconds": 20, "min_improvement": 0.1,
        }
        write_json(root / "experiment.json", spec)
    return root


def write_rows(root, name):
    import json
    rows = [{"id": f"{name}-{i}", "x": i, "y": i * i} for i in range(3)]
    (root / name).write_text(json.dumps(rows), encoding="utf-8")


def write_json(path, value):
    import json
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class MaterializeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        _make_project(self.project)
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_manuscript_md_has_title_abstract_sections(self):
        manuscript = {
            "title": "确定性验证下的可交付稿件",
            "abstract": "本稿件由物化层从已验证 artifact 派生。",
            "sections": [{"heading": "方法", "body": "描述协议。"},
                         {"heading": "结论", "body": "δ 改善阈值达成。"}],
        }
        result = Materializer(self.project).materialize(manuscript, self.out, template="md")
        self.assertEqual("materialized", result["status"])
        path = Path(result["manuscript"])
        self.assertEqual("manuscript.md", path.name)
        self.assertEqual("md", result["template"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("确定性验证下的可交付稿件", text)
        self.assertIn("Abstract", text)
        self.assertIn("## 方法", text)
        self.assertIn("## 结论", text)

    def test_manuscript_tex_renders_structure(self):
        manuscript = {"title": "骨架", "abstract": "摘要摘要",
                      "sections": [{"heading": "方法", "body": "正文"}]}
        result = Materializer(self.project).materialize(manuscript, self.out, "tex")
        text = Path(result["manuscript"]).read_text(encoding="utf-8")
        self.assertIn(r"\title{骨架}", text)
        self.assertIn(r"\begin{abstract}", text)
        self.assertIn(r"\section{方法}", text)

    def test_manuscript_default_disclosure_is_nature(self):
        manuscript = {"title": "标题", "abstract": "摘要", "sections": []}
        result = Materializer(self.project).materialize(manuscript, self.out)
        self.assertEqual("nature", result["disclosure"])
        text = Path(result["manuscript"]).read_text(encoding="utf-8")
        self.assertIn("AI 使用披露", text)
        self.assertIn("自然", text)

    def test_manuscript_disclosure_acm_and_ieee(self):
        manuscript = {"title": "标题", "abstract": "摘要", "sections": []}
        for key, keyword in (("acm", "ACM"), ("ieee", "IEEE")):
            result = Materializer(self.project).materialize(manuscript, self.out, disclosure=key)
            self.assertEqual(key, result["disclosure"])
            text = Path(result["manuscript"]).read_text(encoding="utf-8")
            self.assertIn("AI 使用披露", text)
            self.assertIn(keyword, text)

    def test_manuscript_disclosure_none_omits_declaration(self):
        manuscript = {"title": "标题", "abstract": "摘要", "sections": []}
        result = Materializer(self.project).materialize(manuscript, self.out, disclosure=None)
        self.assertIsNone(result["disclosure"])
        text = Path(result["manuscript"]).read_text(encoding="utf-8")
        self.assertNotIn("AI 使用披露", text)

    def test_manuscript_tex_disclosure_before_end_document(self):
        manuscript = {"title": "骨架", "abstract": "摘要摘要", "sections": []}
        result = Materializer(self.project).materialize(manuscript, self.out, "tex")
        text = Path(result["manuscript"]).read_text(encoding="utf-8")
        self.assertIn(r"\section*{AI 使用披露}", text)
        self.assertLess(text.index(r"\section*{AI 使用披露}"), text.index(r"\end{document}"))

    def test_manuscript_list_input_raises_protocol_error(self):
        with self.assertRaisesRegex(ProtocolError, "单次仅处理一篇稿件"):
            Materializer(self.project).materialize([{"title": "标题"}], self.out)

    def test_manuscript_docx_is_valid_ooxml(self):
        manuscript = {"title": "可交付稿件标题",
                      "abstract": "本稿件由物化层派生。",
                      "sections": [{"heading": "方法", "body": "描述协议。"}]}
        result = Materializer(self.project).materialize(manuscript, self.out, "docx")
        self.assertEqual("docx", result["template"])
        path = Path(result["manuscript"])
        self.assertEqual("manuscript.docx", path.name)
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            self.assertEqual({"word/document.xml", "word/styles.xml", "word/_rels/document.xml.rels",
                              "_rels/.rels", "[Content_Types].xml"}, names)
            doc = archive.read("word/document.xml").decode("utf-8")
        # 标题用 Title 样式；章节用 Heading2；正文含转义后的标题文本与披露。
        self.assertIn('<w:pStyle w:val="Title"/>', doc)
        self.assertIn('<w:pStyle w:val="Heading2"/>', doc)
        self.assertIn("可交付稿件标题", doc)
        self.assertIn("描述协议。", doc)
        self.assertIn("AI 使用披露", doc)  # 默认 nature 披露

    def test_manuscript_pdf_is_valid_single_embedded_font(self):
        manuscript = {"title": "假设与统计口径",
                      "abstract": "评估协议采用预注册阈值与固定数据划分。",
                      "sections": [{"heading": "方法", "body": "δ 改善阈值达成。"}]}
        result = Materializer(self.project).materialize(manuscript, self.out, "pdf")
        self.assertEqual("pdf", result["template"])
        raw = Path(result["manuscript"]).read_bytes()
        # 是合法 PDF（文件头/尾、字体对象、Identity-H 编码、分页对象）。
        self.assertTrue(raw.startswith(b"%PDF-1.4"))
        self.assertTrue(raw.rstrip().endswith(b"%%EOF"))
        for token in (b"/Type /Catalog", b"/Type /Pages", b"/Type /Page ",
                      b"/Type0", b"/CIDFontType2", b"/FontFile2", b"/Identity-H", b"/ToUnicode"):
            self.assertIn(token, raw, token)
        # ToUnicode 必须覆盖正文用到的字形（证明中文字符码有可抽取映射）。
        self.assertIn(b"beginbfchar", raw)
        # 内嵌的应是子集字体（大幅小于全字体内的 20MB），且可被重新解析并含正文汉字。
        i = raw.index(b"8 0 obj")
        seg = raw[i:i + 2_000_000]
        stream_start = seg.index(b"stream\n") + 7
        stream_end = seg.index(b"\nendstream", stream_start)
        subset = seg[stream_start:stream_end]
        self.assertLess(len(subset), 200_000)
        from popper.pdfgen import _TTFont
        font = _TTFont(subset)
        self.assertLess(font.num_glyphs, 500)  # 子集只保留用到的字形
        self.assertTrue(font.glyph_map.get(ord("假")))   # 标题汉字
        self.assertTrue(font.glyph_map.get(ord("δ")))    # 正文希腊字母

    def test_build_repro_package_contains_controlled_deliverables(self):
        result = Materializer(self.project).build_repro_package(self.out)
        self.assertEqual("built", result["status"])
        zip_path = Path(result["path"])
        self.assertEqual("reproducibility.zip", zip_path.name)
        with ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            self.assertIn("experiment.json", names)
            self.assertIn("model.py", names)
            self.assertIn("train.json", names)
            self.assertIn("run.sh", names)
            self.assertIn("README_REPRO.md", names)
            lock = __import__("json").loads(archive.read("environment.lock"))
            self.assertTrue(lock.get("_generated_stub"))

    def test_build_without_experiment_json_raises_protocol_error(self):
        missing = self.root / "missing"
        missing.mkdir()
        _make_project(missing, include_experiment=False)
        with self.assertRaisesRegex(ProtocolError, "experiment.json"):
            Materializer(missing).build_repro_package(self.out)

    def test_self_contained_verify_claims_runs_standalone(self):
        # 完成实验（含 .popper 状态）的聚合结果会随包发出 results.json + verify_claims.py。
        # 这里用一个带 .popper/state.db 的已完成项目来走通自包含核验，且不依赖宿主 Popper 状态。
        import json as _json
        import runpy
        import shutil as _shutil
        import subprocess
        import sys
        from popper.core import Experiment, initialize

        project = self.root / "completed"
        project.mkdir()
        ex = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
        for name in ("experiment.json", "model.py"):
            _shutil.copyfile(ex / name, project / name)
        runpy.run_path(str(ex / "generate_data.py"))["generate"](project)
        initialize(project)
        exp = Experiment(project)
        try:
            exp.search(True)
            exp.freeze()
            exp.confirm(True)
            # 捕获真实冻结候选与 claim δ（Test split 的描述性阈值结论）。
            true_claim = exp.state()["claim"]
        finally:
            exp.close()
        pack = Materializer(project).build_repro_package(self.out)
        with ZipFile(Path(pack["path"])) as archive:
            self.assertIn("results.json", archive.namelist())
            self.assertIn("verify_claims.py", archive.namelist())
            archive.extractall(self.root / "extracted")
        # 在全新解压目录中运行自包含核验（无宿主 .popper 状态）。
        extracted = self.root / "extracted"
        # 核验脚本被发给任意用户，字节编码不能随宿主 locale 变。这里刻意剥掉
        # PYTHONIOENCODING / PYTHONUTF8（等价于中文 Windows 上直接 python verify_claims.py，
        # 子进程按 cp936 输出），并把父进程解码钉在 UTF-8——两侧任何一边「靠 locale」都会被抓。
        child_env = {k: v for k, v in os.environ.items()
                     if k.upper() not in {"PYTHONIOENCODING", "PYTHONUTF8"}}
        run = subprocess.run([sys.executable, "verify_claims.py"], cwd=extracted,
                             capture_output=True, text=True, encoding="utf-8",
                             env=child_env)
        self.assertEqual(0, run.returncode, run.stderr)
        out = _json.loads(run.stdout)
        self.assertIn("delta", out)
        self.assertIn("verified", out)
        # 独立核验的 δ 必须与真实冻结 claim 的 test δ 一致（min 方向、真实候选）。
        self.assertAlmostEqual(out["delta"], true_claim["delta"], places=6)


if __name__ == "__main__":
    unittest.main()