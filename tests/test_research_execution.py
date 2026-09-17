import sys
import unittest

from popper.core import ProtocolError
from popper.research.execution import (EXECUTED_COVERAGE, assess_execution,
                                      changed_statement_lines)


def completed_trace(lines=None):
    return {
        "schema_version": "1.0",
        "completed": True,
        "lines": {} if lines is None else lines,
    }


def trace_source(source, filename="model.py"):
    """Collect actual Python line events, including function definition events."""
    executed = set()

    def record(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == filename:
            executed.add(frame.f_lineno)
        return record

    previous = sys.gettrace()
    try:
        sys.settrace(record)
        exec(compile(source, filename, "exec"), {})
    finally:
        sys.settrace(previous)
    return completed_trace({filename: sorted(executed)})


class ChangedStatementTests(unittest.TestCase):
    def test_changed_prediction_marks_return_without_function_definition(self):
        before = "def predict(x):\n    return x\n\nprediction = predict(2)\n"
        after = "def predict(x):\n    return x * 2\n\nprediction = predict(2)\n"
        self.assertEqual([2], changed_statement_lines(before, after))

    def test_added_uncalled_function_marks_body_without_definition(self):
        before = "prediction = 1\n"
        after = "prediction = 1\n\ndef unused(x):\n    return x * 2\n"
        self.assertEqual([4], changed_statement_lines(before, after))

    def test_changed_method_marks_body_without_class_or_function_definition(self):
        before = "class Model:\n    def predict(self, x):\n        return x\n"
        after = "class Model:\n    def predict(self, x):\n        return x * 2\n"
        self.assertEqual([3], changed_statement_lines(before, after))

    def test_unchanged_control_flow_container_is_not_a_changed_statement(self):
        before = "if True:\n    prediction = 1\n"
        after = "if True:\n    prediction = 2\n"
        self.assertEqual([2], changed_statement_lines(before, after))

    def test_comments_and_whitespace_do_not_create_executable_change(self):
        before = "def predict(x):\n    return x + 1\n"
        after = "# Improved prediction\n\ndef predict( x ):\n    return (x + 1)  # same code\n"
        self.assertEqual([], changed_statement_lines(before, after))

    def test_docstrings_do_not_create_executable_change(self):
        before = '"""Original module docs."""\ndef predict(x):\n    """Original docs."""\n    return x\n'
        after = '"""Updated module docs."""\ndef predict(x):\n    """Updated docs."""\n    return x\n'
        self.assertEqual([], changed_statement_lines(before, after))

    def test_added_docstring_does_not_mark_shifted_body_as_changed(self):
        before = "def predict(x):\n    return x\n"
        after = 'def predict(x):\n    """Predict the response."""\n    return x\n'
        self.assertEqual([], changed_statement_lines(before, after))

    def test_new_helper_file_has_executable_body_lines(self):
        after = "def transform(x):\n    return x * 2\n"
        self.assertEqual([2], changed_statement_lines("", after))

    def test_changed_lines_are_sorted_and_unique(self):
        before = "a = 1; b = 2\nc = 3\n"
        after = "a = 4; b = 5\nc = 6\n"
        self.assertEqual([1, 2], changed_statement_lines(before, after))


class ExecutionAssessmentTests(unittest.TestCase):
    def assert_assessment(self, assessment, passed, reason, coverage, uncovered=None):
        self.assertIs(passed, assessment["passed"])
        self.assertEqual(reason, assessment["reason"])
        self.assertEqual(coverage, assessment["coverage"])
        self.assertEqual([] if uncovered is None else uncovered, assessment["uncovered_files"])
        self.assertEqual("in_process_trace", assessment["trust"])
        self.assertIs(False, assessment["scientific_mechanism_verified"])
        self.assertIsInstance(assessment["files"], dict)

    def test_executed_prediction_change_passes(self):
        before = "def predict(x):\n    return x\n\nprediction = predict(2)\n"
        after = "def predict(x):\n    return x * 2\n\nprediction = predict(2)\n"
        changes = {"model.py": changed_statement_lines(before, after)}
        assessment = assess_execution(changes, trace_source(after))
        self.assert_assessment(assessment, True, "executed_changed_code", "complete")
        self.assertIn("model.py", assessment["files"])

    def test_uncalled_function_fails_despite_executed_definition_line(self):
        before = "prediction = 1\n"
        after = "prediction = 1\n\ndef unused(x):\n    return x * 2\n"
        trace = trace_source(after)
        self.assertIn(3, trace["lines"]["model.py"])
        self.assertNotIn(4, trace["lines"]["model.py"])
        changes = {"model.py": changed_statement_lines(before, after)}
        self.assert_assessment(
            assess_execution(changes, trace), False, "changed_code_not_executed", "none",
            uncovered=["model.py"])

    def test_single_line_definition_change_is_recorded_as_ambiguous(self):
        after = "def helper(): return 42\n"
        trace = trace_source(after, "helpers.py")
        self.assertIn(1, trace["lines"]["helpers.py"])
        changes = {"helpers.py": changed_statement_lines("", after)}
        self.assertEqual([], changes["helpers.py"])
        # The trace proves only the definition event; the body stays unobservable, so
        # the assessment reports evidence instead of claiming the change executed.
        self.assert_assessment(
            assess_execution(changes, trace), True, "no_executable_change", "ambiguous")

    def test_unused_new_helper_file_fails(self):
        changes = {"helpers.py": changed_statement_lines(
            "", "def transform(x):\n    return x * 2\n")}
        trace = trace_source("prediction = 1\n")
        self.assert_assessment(
            assess_execution(changes, trace), False, "changed_code_not_executed", "none",
            uncovered=["helpers.py"])

    def test_untaken_changed_branch_fails_even_when_container_executes(self):
        before = "if False:\n    prediction = 1\n"
        after = "if False:\n    prediction = 2\n"
        changes = {"model.py": changed_statement_lines(before, after)}
        self.assert_assessment(
            assess_execution(changes, trace_source(after)),
            False, "changed_code_not_executed", "none", uncovered=["model.py"])

    def test_untaken_single_line_branch_is_recorded_as_ambiguous(self):
        before = "if False: prediction = 1\n"
        after = "if False: prediction = 2\n"
        trace = trace_source(after)
        self.assertIn(1, trace["lines"]["model.py"])
        changes = {"model.py": changed_statement_lines(before, after)}
        self.assertEqual([], changes["model.py"])
        self.assert_assessment(
            assess_execution(changes, trace), True, "no_executable_change", "ambiguous")

    def test_partial_coverage_is_evidence_not_a_block(self):
        changes = {"model.py": [2], "helpers.py": [4]}
        assessment = assess_execution(changes, completed_trace({"model.py": [1, 2]}))
        self.assert_assessment(
            assessment, True, "partially_executed_changed_code", "partial",
            uncovered=["helpers.py"])
        self.assertEqual([2], assessment["files"]["model.py"]["executed_changed_lines"])
        self.assertEqual([], assessment["files"]["helpers.py"]["executed_changed_lines"])

    def test_all_changed_files_hit_is_complete_coverage(self):
        changes = {"model.py": [2], "helpers.py": [4]}
        self.assert_assessment(
            assess_execution(changes, completed_trace({"model.py": [2], "helpers.py": [4]})),
            True, "executed_changed_code", "complete")

    def test_executed_coverage_constant_matches_real_hits(self):
        # The strict acceptance harnesses rely on this constant to tell observed
        # execution apart from "nothing was detectable", so it has to agree with the
        # verdict: only a completed trace with at least one changed-statement hit counts.
        cases = {
            "complete": ({"model.py": [2]}, completed_trace({"model.py": [2]})),
            "partial": ({"model.py": [2], "helpers.py": [4]},
                        completed_trace({"model.py": [2]})),
            "ambiguous": ({}, completed_trace({})),
            "none": ({"model.py": [2]}, completed_trace({})),
            "unknown": ({"model.py": [2]},
                        {**completed_trace({"model.py": [2]}), "completed": False}),
        }
        for coverage, (changes, trace) in cases.items():
            with self.subTest(coverage=coverage):
                assessment = assess_execution(changes, trace)
                self.assertEqual(coverage, assessment["coverage"])
                observed = trace["completed"] and any(
                    row["executed_changed_lines"] for row in assessment["files"].values())
                self.assertIs(coverage in EXECUTED_COVERAGE, observed)

    def test_no_executed_changed_statement_blocks(self):
        changes = {"model.py": [2], "helpers.py": [4]}
        self.assert_assessment(
            assess_execution(changes, completed_trace({"other.py": [1]})),
            False, "changed_code_not_executed", "none",
            uncovered=["helpers.py", "model.py"])

    def test_one_executed_changed_statement_is_enough(self):
        self.assert_assessment(
            assess_execution({"model.py": [2, 3]}, completed_trace({"model.py": [3]})),
            True, "executed_changed_code", "complete")

    def test_files_without_executable_changes_do_not_require_trace_hits(self):
        self.assert_assessment(
            assess_execution({"model.py": [2], "docs.py": []},
                             completed_trace({"model.py": [2]})),
            True, "executed_changed_code", "complete")

    def test_comments_only_change_is_recorded_as_ambiguous(self):
        before = "prediction = 1\n"
        after = "# Better model\nprediction = 1\n"
        changes = {"model.py": changed_statement_lines(before, after)}
        self.assert_assessment(
            assess_execution(changes, trace_source(after)),
            True, "no_executable_change", "ambiguous")

    def test_empty_changes_are_ambiguous_not_a_failure(self):
        self.assert_assessment(
            assess_execution({}, completed_trace({"model.py": [1]})),
            True, "no_executable_change", "ambiguous")

    def test_no_recorded_execution_fails(self):
        self.assert_assessment(
            assess_execution({"model.py": [2]}, completed_trace()),
            False, "changed_code_not_executed", "none", uncovered=["model.py"])

    def test_incomplete_trace_fails_even_with_changed_statement_hit(self):
        trace = completed_trace({"model.py": [2]})
        trace["completed"] = False
        self.assert_assessment(
            assess_execution({"model.py": [2]}, trace),
            False, "trace_incomplete", "unknown")

    def test_malformed_traces_raise_protocol_error(self):
        invalid_traces = [
            None,
            {},
            [],
            {"schema_version": "2.0", "completed": True, "lines": {}},
            {"schema_version": "1.0", "completed": True},
            {"schema_version": "1.0", "lines": {}},
            {"schema_version": "1.0", "completed": "true", "lines": {}},
            {"schema_version": "1.0", "completed": True, "lines": []},
            completed_trace({"model.py": "2"}),
            completed_trace({"model.py": ["2"]}),
            completed_trace({"model.py": [True]}),
            completed_trace({"model.py": [0]}),
            completed_trace({"model.py": [-1]}),
        ]
        for trace in invalid_traces:
            with self.subTest(trace=trace):
                with self.assertRaises(ProtocolError):
                    assess_execution({"model.py": [2]}, trace)


if __name__ == "__main__":
    unittest.main()
