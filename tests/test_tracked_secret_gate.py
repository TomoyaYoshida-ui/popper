import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check_tracked_secrets.py"
SPEC = importlib.util.spec_from_file_location("tracked_secret_gate", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TrackedSecretGateTests(unittest.TestCase):
    def test_detects_common_credentials_without_returning_values(self):
        secret = b"sk-" + b"a" * 30
        self.assertEqual(["api_key"], MODULE.content_issues(secret))
        self.assertEqual(["private_key"], MODULE.content_issues(
            b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----\nexample"))

    def test_blocks_sensitive_filenames(self):
        for name in (".env", ".env.local", "runner.pem", "holdout.sqlite", "id_ed25519"):
            with self.subTest(name=name):
                self.assertIsNotNone(MODULE.path_issue(Path(name)))
        self.assertIsNone(MODULE.path_issue(Path("protocol.json")))


if __name__ == "__main__":
    unittest.main()
