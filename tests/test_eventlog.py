import unittest

from popper.core import digest
from popper.eventlog import GENESIS_HASH, chained_body, verify_hash_chain


class EventLogTests(unittest.TestCase):
    def test_chained_body_preserves_existing_hash_contract(self):
        fields = {"event_type": "created", "payload": {"value": 1}}
        body, event_hash = chained_body(fields, GENESIS_HASH)
        self.assertEqual(GENESIS_HASH, body["previous_hash"])
        self.assertEqual(digest({**fields, "previous_hash": GENESIS_HASH}), event_hash)

    def test_verifies_global_chain_and_reports_head(self):
        records = []
        previous = GENESIS_HASH
        for seq, event_type in enumerate(("created", "completed"), 1):
            fields = {"event_type": event_type, "payload": {"seq": seq}}
            _, event_hash = chained_body(fields, previous)
            records.append({"seq": seq, **fields, "previous_hash": previous,
                            "hash": event_hash})
            previous = event_hash
        result = verify_hash_chain(
            records,
            lambda row: {"event_type": row["event_type"], "payload": row["payload"]},
            lambda row: row["previous_hash"], lambda row: row["hash"],
            seq_for=lambda row: row["seq"])
        self.assertEqual({"ok": True, "events": 2, "head": previous}, result)

    def test_tamper_and_broken_link_are_rejected(self):
        fields = {"event_type": "created", "payload": {"value": 1}}
        _, event_hash = chained_body(fields, GENESIS_HASH)
        record = {"seq": 7, **fields, "previous_hash": GENESIS_HASH,
                  "hash": event_hash}
        record["payload"] = {"value": 2}
        result = verify_hash_chain(
            [record],
            lambda row: {"event_type": row["event_type"], "payload": row["payload"]},
            lambda row: row["previous_hash"], lambda row: row["hash"],
            label="fixture event", seq_for=lambda row: row["seq"])
        self.assertFalse(result["ok"])
        self.assertIn("seq=7", result["reason"])

    def test_supports_legacy_previous_field_name(self):
        fields = {"kind": "study", "payload": {}}
        body, event_hash = chained_body(fields, GENESIS_HASH, previous_key="previous")
        self.assertEqual(GENESIS_HASH, body["previous"])
        self.assertEqual(digest({**fields, "previous": GENESIS_HASH}), event_hash)


if __name__ == "__main__":
    unittest.main()
