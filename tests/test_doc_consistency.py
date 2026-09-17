"""文档口径与实现一致性的机器检查（漂移门禁）。

`docs/技术方案.md` 里形如 ``<!-- ci-check: name=value -->`` 的标记是**可被机器校验的承诺**：
改了实现却忘了改文档（或反之）都会在这里失败。新增这类承诺时按同一格式加标记。

`docs/实施记录.md` 用同一套标记锁住**评估产物里的数字**（见 BLIND_MARKERS）：叙述性统计
（多少条轨迹证据有效、三臂各命中几格）过去只靠人抄，抄错（哪怕只是低报）就是文档漂移；
现在改报告不改文档会直接红灯。

这批检查是批次 E8 的出口条件：把「文档承诺 > 实现」从人工发现的漂移，变成 CI 里的红灯。
"""
import json
import re
import unittest
from pathlib import Path

from popper.core import EVALUATORS
from popper.isolation import isolation_status

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "技术方案.md"
RECORD = REPO / "docs" / "实施记录.md"
MARKER = re.compile(r"<!--\s*ci-check:\s*([a-z_]+)\s*=\s*(\S+?)\s*-->")
SECTION_REF = re.compile(r"§\s*(\d+(?:\.\d+)?)")
HEADING = re.compile(r"(?m)^#{2,4}\s+(\d+(?:\.\d+)?)\b")

BLIND_REPORT = (REPO / "evaluation" / "runs" / "bp-20260913T083501Z-c32a"
                / "blind-repair-report.json")
# 标记名 -> 产物内的取值路径；路径末项 "__len__" 表示取该列表的长度。映射放在代码里而不是
# 文档里，是为了避免「文档自己跟自己校对」：文档只能声明数值，取哪个字段由这里说了算。
BLIND_MARKERS = {
    "blind_planned_trajectories": ("planned_trajectories",),
    "blind_valid_loops": ("valid_loops",),
    "blind_conclusion_matched": ("conclusion_matched",),
    "blind_scientifically_valid_loops": ("scientifically_valid_loops",),
    "blind_replacement_count": ("composite", "replacement_count"),
    "blind_original_failures": ("composite", "original_failures", "__len__"),
    "blind_arm_adaptive": ("by_comparator", "adaptive_research_controller",
                           "conclusion_matched"),
    "blind_arm_fixed_search": ("by_comparator", "fixed_registered_search",
                               "conclusion_matched"),
    "blind_arm_same_model_fixed": ("by_comparator", "same_model_same_tools_fixed_plan",
                                   "conclusion_matched"),
    "blind_condition_positive": ("by_condition", "positive_effect", "conclusion_matched"),
    "blind_condition_negative": ("by_condition", "negative_effect", "conclusion_matched"),
    "blind_condition_near_zero": ("by_condition", "near_zero_effect", "conclusion_matched"),
    "blind_condition_boundary": ("by_condition", "scope_boundary_or_counterexample",
                                 "conclusion_matched"),
}


def markers_in(path):
    """解析文档里的机器可校验标记；同名标记重复即视为文档写坏。"""
    found = {}
    for name, value in MARKER.findall(path.read_text(encoding="utf-8")):
        if name in found:
            raise AssertionError(f"{path.name} 出现重复的 ci-check 标记: {name}")
        found[name] = value
    return found


def doc_markers():
    return markers_in(DOC)


def all_markers():
    """两份文档共享一个标记命名空间：同一个承诺只能被一份文档声明一次。"""
    merged = {}
    for path in (DOC, RECORD):
        for name, value in markers_in(path).items():
            if name in merged:
                raise AssertionError(f"ci-check 标记 {name} 在两份文档里重复声明")
            merged[name] = value
    return merged


def report_value(report, path):
    value = report
    for key in path:
        value = len(value) if key == "__len__" else value[key]
    return value


class DocConsistencyTests(unittest.TestCase):
    def test_required_markers_are_present(self):
        found = doc_markers()
        for name in ("isolation_items", "evaluators"):
            self.assertIn(name, found, f"docs/技术方案.md 缺少 ci-check: {name} 标记")
        self.assertTrue(int(found["isolation_items"]) > 0)
        self.assertNotIn(" ", found["evaluators"])

    def test_isolation_item_count_matches_popper_isolation(self):
        declared = int(doc_markers()["isolation_items"])
        report = isolation_status(".")
        self.assertEqual(declared, len(report["items"]),
                         "docs/技术方案.md 声明的隔离项数与 popper isolation 输出不一致")
        self.assertEqual(declared, report["summary"]["total"])

    def test_evaluator_registry_matches_doc(self):
        declared = doc_markers()["evaluators"].split(",")
        self.assertEqual(len(declared), len(set(declared)), "文档里的评估契约有重复")
        self.assertEqual(sorted(declared), sorted(EVALUATORS),
                         "docs/技术方案.md 声明的评估契约与域包注册表不一致")

    def test_blind_report_numbers_in_record_match_the_artifact(self):
        """实施记录里被标记锁住的盲测数字必须逐个等于产物实算值。"""
        self.assertTrue(BLIND_REPORT.is_file(),
                        f"缺少 {BLIND_REPORT.relative_to(REPO).as_posix()}；.gitignore 有意保留 "
                        "evaluation/runs/*/*.json（结论报告），删掉它等于删掉门禁的证据源")
        report = json.loads(BLIND_REPORT.read_text(encoding="utf-8"))
        declared = all_markers()
        for name, path in BLIND_MARKERS.items():
            self.assertIn(name, declared,
                          f"两份 docs 缺少 ci-check: {name} 标记（不得静默丢掉已门禁的数字）")
            actual = report_value(report, path)
            self.assertEqual(int(declared[name]), actual,
                             f"文档里的 {name}={declared[name]} 与产物实算 {actual} 不一致")

    def test_blind_report_is_internally_consistent(self):
        """产物自身也要自洽：三臂与四种条件各自求和都得等于结论命中数与轨迹总数。"""
        if not BLIND_REPORT.is_file():
            self.skipTest(f"{BLIND_REPORT.name} 不在本机")
        report = json.loads(BLIND_REPORT.read_text(encoding="utf-8"))
        arms, conditions = report["by_comparator"], report["by_condition"]
        self.assertEqual(sum(a["conclusion_matched"] for a in arms.values()),
                         report["conclusion_matched"])
        self.assertEqual(sum(c["conclusion_matched"] for c in conditions.values()),
                         report["conclusion_matched"])
        self.assertEqual(sum(a["planned"] for a in arms.values()), report["planned_trajectories"])
        self.assertEqual(sum(c["planned"] for c in conditions.values()),
                         report["planned_trajectories"])
        self.assertLessEqual(report["conclusion_matched"], report["valid_loops"])
        self.assertLessEqual(report["valid_loops"], report["planned_trajectories"])
        self.assertFalse(report["composite"]["is_single_frozen_run"],
                         "该报告不再 post_fix_composite，实施记录里的描述需重写")
        self.assertFalse(report["claims"]["adaptive_gain_claimed"],
                         "产物不再声明自适应增益为否，文档的保守口径需同步上调")
        self.assertFalse(report["claims"]["scientific_accuracy_claimed"])

    def test_section_references_point_at_a_real_heading(self):
        """形如 §9.4 的引用必须在技术方案.md 里能找到对应小节。悬空章节号是文档漂移里
        最低成本、最容易修、也最容易让读者误判的一类，因此纳入门禁。"""
        targets = set(HEADING.findall(DOC.read_text(encoding="utf-8")))
        self.assertTrue(targets, "技术方案.md 没有任何带编号的小节？")
        for path in (DOC, RECORD):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                for ref in SECTION_REF.findall(line):
                    self.assertIn(ref, targets,
                                  f"{path.name}:{lineno} 引用了不存在的 §{ref}")


if __name__ == "__main__":
    unittest.main()
