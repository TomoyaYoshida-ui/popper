"""物化层 + 复现包。把已验证的科研 artifact 转成可交付稿件与可一键重跑的复现包。"""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .core import ProtocolError, inside, read_json


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
    def build_repro_package(self, out_dir):
        if not (self.root / "experiment.json").is_file():
            raise ProtocolError("复现包必须含 experiment.json；项目目录缺少受控实验配置")
        spec = read_json(self.root / "experiment.json")
        if not isinstance(spec, dict):
            raise ProtocolError("experiment.json 必须包含规范的实验配置")
        code_files = spec.get("code_files")
        if not isinstance(code_files, list):
            raise ProtocolError("experiment.json 缺少 code_files")
        for name in ["experiment.json", *code_files, spec.get("train"), spec.get("dev"),
                     spec.get("test")]:
            # 越界检查必须走 inside()：它对拼接结果先 resolve() 再判前缀。原先写成
            # `(self.root / name).is_relative_to(self.root)`，而 resolve() 之前的 `..`
            # 不折叠（`C:\proj\..\secret\x.json` 仍以前缀 `C:\proj` 开头），守卫对任何
            # `../` 都恒真——形同虚设，项目外的文件会被打进 reproducibility.zip。
            inside(self.root, name)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        zip_path = out_dir / "reproducibility.zip"
        files = ["experiment.json", *code_files, spec["train"], spec["dev"], spec["test"]]
        if spec.get("provenance_files"):
            files.extend(spec["provenance_files"])
        lock_present = (self.root / "environment.lock").is_file()
        # 聚合已完成运行结果，使自包含核验脚本可独立重算 claim（不依赖目标 .popper）。
        aggregated = self._aggregate_results()
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
                archive.writestr("verify_claims.py",
                                 self._verify_script(spec, aggregated.get("selected")))
                files.append("verify_claims.py")
            archive.writestr("run.sh", RUN_SCRIPT)
            archive.writestr("README_REPRO.md", self._readme(lock_present, aggregated is not None))
            files.extend(["run.sh", "README_REPRO.md"])
        with ZipFile(zip_path) as check:
            if "experiment.json" not in check.namelist():
                raise ProtocolError("复现包校验失败：缺少 experiment.json")
        return {"status": "built", "path": str(zip_path), "files": files}

    def _aggregate_results(self):
        """从已完成实验的 .popper 状态聚合 runs 结果；未完成/无状态则返回 None。

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
                if state.get("phase") not in {"frozen", "completed", "confirming", "confirmation_failed"}:
                    return None
                results = exp.results()
                selected = state.get("selected")
            finally:
                exp.close()
            # 聚合为 verify_claims.py 期望的扁平 runs 结构（含 config/mean）+ 冻结候选。
            return {"schema_version": "1.0", "selected": selected, "runs": [
                {"config": r["config"], "split": r["split"], "mean": r["mean"]}
                for r in results]}
        except Exception:
            return None

    def _verify_script(self, spec, selected_config=None):
        metric = spec["metric"]["name"]
        direction = spec["metric"]["direction"]
        threshold = spec["min_improvement"]
        # selected_config：真实冻结候选；缺省则回退为"首个非 baseline"（尽力而为，
        # 但应答验时传入真值，避免误选）。summary json.dumps 不再 !r 二次转义。
        metric_lit = json.dumps(metric, ensure_ascii=False)
        direction_lit = json.dumps(direction, ensure_ascii=False)
        threshold_lit = json.dumps(threshold, ensure_ascii=False)
        selected_lit = json.dumps(selected_config, ensure_ascii=False) if selected_config is not None else "None"
        return "\n".join([
            "#!/usr/bin/env python3",
            "'''自包含离线核验：仅依赖本包内 experiment.json + results.json。",
            "确定性重算 claim（描述性阈值，不做显著性检验），不依赖宿主 Popper 状态。'''",
            "from __future__ import annotations",
            "import json, pathlib, sys",
            "root = pathlib.Path(__file__).resolve().parent",
            # stdout 是机器解析的契约（含中文 reason/scope），必须与宿主 locale 无关：
            # 重定向到管道时 Python 按 locale 编码（中文 Windows 为 cp936），会让按 UTF-8
            # 读取的一方解码失败。这里显式钉住 UTF-8，与 popper/vendors.py 调用 worker 的口径一致。
            "if hasattr(sys.stdout, 'reconfigure'):",
            "    sys.stdout.reconfigure(encoding='utf-8', errors='replace')",
            "spec = json.loads((root / 'experiment.json').read_text(encoding='utf-8'))",
            "results = json.loads((root / 'results.json').read_text(encoding='utf-8'))",
            "runs = results.get('runs') or results.get('results') or []",
            f"metric = {metric_lit}",
            f"direction = {direction_lit}",
            f"threshold = {threshold_lit}",
            f"selected_config = {selected_lit}",
            "def config_eq(a, b): return a == b or (a is not None and b is not None and str(a) == str(b))",
            "# 复刻 core.confirm 口径：最终测试（test split）的 baseline vs 冻结候选。",
            "test_runs = [r for r in runs if r.get('split') == 'test'] or runs",
            "baseline = next((r for r in test_runs if r.get('config') == spec['baseline']), None)",
            "selected = next((r for r in test_runs if config_eq(r.get('config'), selected_config) "
            "and r['config'] != spec['baseline']), None)",
            "if selected is None:",
            "    selected = next((r for r in test_runs if r.get('config') is not None and r['config'] != spec['baseline']), None)",
            "out = {'metric': metric, 'verified': False}",
            "if not baseline or not selected:",
            "    out['reason'] = '缺少基线结果或候选结果'",
            "else:",
            "    delta = (baseline['mean'] - selected['mean'] if direction == 'min'",
            "             else selected['mean'] - baseline['mean'])",
            "    out['delta'] = delta",
            "    out['threshold'] = threshold",
            "    out['selected_config'] = selected['config']",
            "    out['supports_threshold'] = bool(delta > 0 and delta >= threshold)",
            "    out['verified'] = out['supports_threshold']",
            "    out['scope'] = '仅当前数据划分与 pre-registered 配置；不做显著性检验'",
            "print(json.dumps(out, ensure_ascii=False))",
            "if not out['verified']:",
            "    raise SystemExit(2)",  # 非零退出便于脚本化验收
            "",
        ])

    def _readme(self, lock_present, results_present=False):
        lock_note = ("已在包内含 environment.lock，用于复现时锁定运行环境。"
                     if lock_present else
                     "未找到 environment.lock；包内为 generated_stub 占位，复现前请锁定宿主环境（Python 版本等）。")
        verify_note = ("包内含自包含离线核验脚本 verify_claims.py 与 run.sh，解压后无需宿主 .popper 状态即可确定性重算 claim。"
                       if results_present else
                       "本包未包含 results.json，无法执行自包含离线核验；请在宿主用 popper experiment replay 提供证据。")
        return "\n".join([
            "# Popper 复现包", "",
            "本包由物化层从已验证的科研 artifact 生成，聚焦确定性交付（代码 + 数据 + 环境锁 + 一键重跑）。",
            "", "## 环境锁定", "", lock_note, "",
            "## 一键重跑（自包含离线核验，推荐）", "", verify_note, "",
            "进入解压后的项目目录：", "    python verify_claims.py   # 确定性重算 claim（无需宿主 .popper）",
            "    # 或", "    bash run.sh                  # 优先 verify_claims，成功后再尝试审计工作台",
            "",
            "## 依赖宿主 Popper 的离线审计（可选）", "",
            "若宿主已安装 popper 且项目保有 .popper 状态，可：",
            "    python -m popper experiment replay <project>   # 从已保存预测重算指标与 claim（不重跑代码/LLM）",
            "",
            "## δ 验证口径", "",
            "Popper 采用确定性验证器闭环兑现：claim 只针对 pre-registered 配置、固定数据划分与固定评估器",
            "（见 experiment.json 中 metric / seeds / baseline）。重跑以离线证据重算为准，不做显著性检验，",
            "不证明机制或创新性。任何导致输入/环境/评估器版本变化的修改都会使验证失败。",
            "",
        ])


RUN_SCRIPT = "\n".join([
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    "# Popper 物化层一键重跑入口：优先自包含离线核验（无需宿主 .popper 状态）。",
    "cd \"${1:-.}\"",
    "if [ -f results.json ] && [ -f verify_claims.py ]; then",
    "    echo \"==> 自包含离线核验：python verify_claims.py\"",
    "    python verify_claims.py",
    "    echo \"==> 核验通过（supports_threshold）。\"",
    "    exit 0",
    "fi",
    "echo \"==> 本包未含 results.json，尝试宿主 Popper 离线重算（需宿主已安装 popper 且保有 .popper 状态）。\"",
    "python -m popper experiment replay .",
    "",
])