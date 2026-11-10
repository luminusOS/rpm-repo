from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import retention  # noqa: E402


def ledger(*versions):
    return {"x86_64": {f"p-{v}.rpm": {"version": v} for v in versions}}


class RetentionTests(unittest.TestCase):
    def test_the_newest_three_versions_are_kept_in_version_order_not_name_order(self):
        result = retention.plan(ledger("1.0.5-rc.2", "1.0.5-rc.10", "1.0.5", "1.0.4", "1.0.3"))["x86_64"]
        self.assertEqual(result["keep"], ["p-1.0.5.rpm", "p-1.0.5-rc.10.rpm", "p-1.0.5-rc.2.rpm"])
        self.assertEqual(result["removable"], ["p-1.0.4.rpm", "p-1.0.3.rpm"])

    def test_pinned_files_are_never_removable(self):
        result = retention.plan(ledger("1.0.5", "1.0.4", "1.0.3", "1.0.2", "1.0.1"), ["p-1.0.1.rpm"])["x86_64"]
        self.assertEqual(result["removable"], ["p-1.0.2.rpm"])
        self.assertIn("p-1.0.1.rpm", result["keep"])

    def test_a_short_history_removes_nothing(self):
        self.assertEqual(retention.plan(ledger("1.0.5"))["x86_64"]["removable"], [])


if __name__ == "__main__":
    unittest.main()
