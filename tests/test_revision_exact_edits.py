import unittest

from popper.core import ProtocolError
from popper.revision_tools import TOOL_DEFINITIONS, LEGACY_TOOL_DEFINITIONS, validate
from popper.research.revision_drafts import apply_exact_edits


class RevisionExactEditTests(unittest.TestCase):
    def test_sequential_exact_transaction(self):
        original = "def main():\n    value = 1\n    return value\n"
        edits = [{"search": "value = 1", "replace": "value = 2"},
                 {"search": "return value", "replace": "return value + 1"}]
        self.assertEqual(original.replace("value = 1", "value = 2").replace("return value", "return value + 1"),
                         apply_exact_edits(original, edits))
        self.assertEqual("value = 1", edits[0]["search"])
        self.assertIn("value = 1", original)

    def test_missing_ambiguous_and_invalid_edit_never_partially_apply(self):
        for edits in ([{"search": "missing", "replace": "new"}],
                      [{"search": "x", "replace": "y"}],
                      [{"search": "x = 1", "replace": "x = 2"}, {"search": "missing", "replace": "new"}],
                      [{"search": "", "replace": "new"}],
                      [{"search": "x = 1", "replace": "new", "path": "other.py"}],
                      [{"search": "x = 1", "replace": "new"}] * 21):
            with self.subTest(edits=edits), self.assertRaises(ProtocolError):
                apply_exact_edits("x = 1\nx = 10\n", edits)

    def test_no_fuzzy_matching(self):
        with self.assertRaisesRegex(ProtocolError, "found 0"):
            apply_exact_edits("def main():\n    return 1\n", [{"search": "def main():\n  return 1", "replace": "pass"}])

    def test_utf8_input_and_converted_source_limit(self):
        for source, edits in (("x", [{"search": "x", "replace": "界" * 22000}]),
                              ("prefix" + "z" * 65530, [{"search": "prefix", "replace": "longer-prefix"}])):
            with self.assertRaisesRegex(ProtocolError, "64 KiB"):
                apply_exact_edits(source, edits)

    def test_reference_tools_are_preferred_and_legacy_definitions_retained(self):
        names = {item["function"]["name"] for item in TOOL_DEFINITIONS}
        self.assertEqual({"research_revision_preview", "research_revision_commit", "research_revision_run", "research_revision_job"}, names)
        self.assertEqual({"research_revision_create", "research_revision_execute"},
                         {item["function"]["name"] for item in LEGACY_TOOL_DEFINITIONS})
        for tool, args in (("research.revision_commit", {"study_id": "S-example", "preview_call_id": "not-a-call"}),
                           ("research.revision_run", {"study_id": "S-example", "create_call_id": "not-a-call"})):
            with self.assertRaises(ProtocolError):
                validate(tool, args)
