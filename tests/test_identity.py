import unittest

from popper.core import ProtocolError
from popper.identity import EntityKind, content_id, new_id, parse_id


class IdentityTests(unittest.TestCase):
    def test_new_ids_are_canonical_and_kind_safe(self):
        values = []
        for kind in EntityKind:
            value = new_id(kind)
            values.append(value)
            self.assertEqual(kind, parse_id(value, kind).kind)
            self.assertTrue(parse_id(value).canonical)
            self.assertEqual(30, len(value))
        self.assertEqual(len(EntityKind), len(set(values)))

    def test_content_ids_are_stable_and_canonicalize_mapping_order(self):
        left = content_id(EntityKind.ARTIFACT, {"b": 2, "a": [1, 3]})
        right = content_id("artifact", {"a": [1, 3], "b": 2})
        self.assertEqual(left, right)
        self.assertEqual(EntityKind.ARTIFACT, parse_id(left).kind)

    def test_content_ids_reject_non_json_and_nan(self):
        with self.assertRaises(ProtocolError):
            content_id(EntityKind.ARTIFACT, object())
        with self.assertRaises(ProtocolError):
            content_id(EntityKind.ARTIFACT, {"value": float("nan")})

    def test_legacy_research_ids_remain_readable(self):
        cases = {
            "S-deadbeef": EntityKind.STUDY,
            "H-deadbeef": EntityKind.HYPOTHESIS,
            "H-control-deadbeef": EntityKind.HYPOTHESIS,
            "D-deadbeef": EntityKind.DESIGN,
            "OBS-deadbeef": EntityKind.OBSERVATION,
            "DEC-deadbeef": EntityKind.DECISION,
            "REV-deadbeef": EntityKind.REVISION,
            "RUN-deadbeef": EntityKind.RUN,
            "JOB-deadbeef": EntityKind.JOB,
            "C001": EntityKind.CLAIM,
            "w_deadbeef1234": EntityKind.WORKSPACE,
            "q_0123456789abcdef0123456789abcdef": EntityKind.QUEST,
        }
        for value, kind in cases.items():
            with self.subTest(value=value):
                ref = parse_id(value)
                self.assertEqual(kind, ref.kind)
                self.assertFalse(ref.canonical)

    def test_bare_uuid_requires_run_context(self):
        value = "123e4567-e89b-42d3-a456-426614174000"
        with self.assertRaises(ProtocolError):
            parse_id(value)
        self.assertEqual(EntityKind.RUN, parse_id(value, EntityKind.RUN).kind)

    def test_kind_mismatch_and_paths_fail_closed(self):
        with self.assertRaises(ProtocolError):
            parse_id(new_id(EntityKind.RUN), EntityKind.JOB)
        with self.assertRaises(ProtocolError):
            parse_id("C:/tmp/result.json")
        with self.assertRaises(ProtocolError):
            parse_id("../result.json")
        with self.assertRaises(ProtocolError):
            new_id("not-a-kind")

    def test_primary_workspace_alias_requires_explicit_context(self):
        with self.assertRaises(ProtocolError):
            parse_id("primary")
        ref = parse_id("primary", EntityKind.WORKSPACE)
        self.assertEqual({"kind": "workspace", "id": "primary"}, ref.as_dict())


if __name__ == "__main__":
    unittest.main()
