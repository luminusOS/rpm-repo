import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import descriptors  # noqa: E402


class DescriptorTests(unittest.TestCase):
    def test_checked_in_files_match_the_catalog(self):
        self.assertEqual(descriptors.main(["check"]), 0)

    def test_every_channel_requires_package_and_repository_signatures(self):
        for channel in ("stable", "testing"):
            text = (ROOT / "descriptors" / channel / f"luminus-{channel}.repo").read_text()
            self.assertIn("gpgcheck=1\n", text)
            self.assertIn("repo_gpgcheck=1\n", text)
            self.assertNotIn("gpgcheck=0", text)
            self.assertIn(f"baseurl=https://rpm.luminusos.org/{channel}/45/$basearch/", text)
            self.assertIn(f"gpgkey=https://rpm.luminusos.org/{channel}/luminus.asc", text)

    def test_the_two_channels_use_different_repository_ids(self):
        ids = [(ROOT / "descriptors" / channel / f"luminus-{channel}.repo").read_text().split("]")[0]
               for channel in ("stable", "testing")]
        self.assertEqual(len(set(ids)), 2)

    def test_a_catalog_for_another_release_is_refused(self):
        catalog = json.loads((ROOT / "catalog.json").read_text())
        with self.assertRaises(ValueError):
            descriptors.render("stable", {**catalog, "fedora": 44})


if __name__ == "__main__":
    unittest.main()
