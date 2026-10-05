import json
import unittest

from popper.core import ProtocolError
from popper.tool_results import read_detail, summarize_tool_result


class ToolResultSummaryTests(unittest.TestCase):
    def test_identity_budget_versions_and_observations_are_not_truncated(self):
        observations = [{"observation_id": f"OBS-{i}", "score": i / 17,
                         "payload": {"content": "scientific" * 600}}
                        for i in range(20)]
        original = {"study_id": "S-a", "job": {"job_id": "JOBR-a", "status": "succeeded"},
                    "state_version": 41, "manifest_sha256": "a" * 64,
                    "budget": {"spent": 3, "remaining": 7}, "observations": observations,
                    "diff": "-old\n+new\n" * 700}
        result = summarize_tool_result(original, "call-source")
        for key in ("study_id", "job", "state_version", "manifest_sha256", "budget", "observations"):
            self.assertEqual(original[key], result[key])
        self.assertNotIn("result_excerpt", result)
        reference = result["diff"]["details_reference"]
        self.assertEqual("call-source", reference["source_call_id"])
        self.assertEqual("/diff", reference["field_path"])
        self.assertEqual(original["diff"], read_detail(original, reference["field_path"]))

    def test_large_json_result_without_heavy_fields_is_retained_completely(self):
        original = {"job_id": "JOBR-real", "scores": {f"case-{i}": i / 3 for i in range(2000)}}
        self.assertGreater(len(json.dumps(original)), 4096)
        self.assertEqual(original, summarize_tool_result(original, "call-source"))

    def test_long_nested_details_are_redacted_and_do_not_mutate_execution_result(self):
        original = {"preview": {"a/b~c": {"content": '{"api_key":"hidden"}\n' + "code\n" * 1000}},
                    "status": "completed"}
        result = summarize_tool_result(original, "call-source")
        reference = result["preview"]["a/b~c"]["content"]["details_reference"]
        self.assertEqual("/preview/a~1b~0c/content", reference["field_path"])
        detail = read_detail(original, reference["field_path"])
        self.assertNotIn("hidden", detail)
        self.assertIn("[REDACTED]", detail)
        self.assertIn("hidden", original["preview"]["a/b~c"]["content"])

    def test_array_detail_pointer_and_malformed_pointers(self):
        result = {"logs": ["x" * 5000, "last"]}
        self.assertEqual("last", read_detail(result, "/logs/1"))
        for path in ("logs/1", "/logs/01", "/logs/-1", "/logs/2", "/logs/0/no", "/logs~"):
            with self.subTest(path=path), self.assertRaises(ProtocolError):
                read_detail(result, path)

    def test_scalar_detail_is_explicit_reference_not_excerpt(self):
        source = "line\n" * 2000
        result = summarize_tool_result(source, "call-source")
        self.assertEqual("", result["details_reference"]["field_path"])
        self.assertEqual(source, read_detail(source, ""))
        self.assertEqual(64, len(result["details_reference"]["sha256"]))


if __name__ == "__main__":
    unittest.main()
