import unittest

from popper.api_contracts import ErrorCode, failure, new_trace_id, success
from popper.core import ProtocolError


class ApiContractTests(unittest.TestCase):
    def test_success_envelope(self):
        trace = new_trace_id()
        body = success({"items": []}, trace_id=trace, event_cursor=0)
        self.assertEqual("1.0", body["schema_version"])
        self.assertTrue(body["ok"])
        self.assertEqual(trace, body["meta"]["trace_id"])
        self.assertEqual(0, body["meta"]["event_cursor"])

    def test_failure_envelope_uses_stable_code(self):
        trace = new_trace_id()
        body = failure(ErrorCode.INVALID_TRANSITION, "design frozen",
                       trace_id=trace, details={"design_id": "D-1"})
        self.assertFalse(body["ok"])
        self.assertEqual("invalid_transition", body["error"]["code"])
        self.assertFalse(body["error"]["retryable"])

    def test_contract_rejects_unknown_codes_and_bad_metadata(self):
        trace = new_trace_id()
        with self.assertRaises(ProtocolError):
            failure("made_up", "x", trace_id=trace)
        with self.assertRaises(ProtocolError):
            success({}, trace_id="bad")
        with self.assertRaises(ProtocolError):
            success({}, trace_id=trace, event_cursor=-1)


if __name__ == "__main__":
    unittest.main()
