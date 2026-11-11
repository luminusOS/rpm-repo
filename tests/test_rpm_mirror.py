import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import issuer  # noqa: E402
import rpm_import  # noqa: E402
import rpm_mirror  # noqa: E402
from rpm_import import ImporterError  # noqa: E402

DATA = b"rpm bytes"
DIGEST = hashlib.sha256(DATA).hexdigest()


def pin(**changes):
    entry = {"name": "tool", "version": "1.0.0", "release": "1", "arch": "x86_64",
             "url": "https://github.com/luminusOS/tool/releases/download/v1.0.0/tool-1.0.0-1.x86_64.rpm",
             "sha256": DIGEST, "license": "MIT"}
    return {**entry, **changes}


def package(entry, data=DATA):
    return {"entry": entry, "data": data, "sha256": hashlib.sha256(data).hexdigest()}


class PinTests(unittest.TestCase):
    def write(self, packages):
        directory = tempfile.mkdtemp()
        (Path(directory) / "mirror.json").write_text(json.dumps({"schema_version": 1, "packages": packages}))
        return Path(directory)

    def test_the_committed_pins_are_valid_and_cover_the_four_packages(self):
        pins = rpm_mirror.load_pins(ROOT)
        self.assertEqual({entry["name"] for entry in pins},
                         {"elastic-swap", "gnome-shell-extension-aurora-shell", "appimage-thumbnailer",
                          "ddcutil-service"})
        self.assertEqual([entry["arch"] for entry in pins if entry["name"].startswith("gnome-shell")], ["noarch"])

    def test_pins_outside_the_host_allowlist_or_with_bad_fields_are_refused(self):
        for bad in (pin(url="http://github.com/x.rpm"), pin(url="https://example.com/x.rpm"),
                    pin(url="https://user@github.com/x.rpm"), pin(url="https://github.com/x.rpm?a=1"),
                    pin(sha256="abc"), pin(arch="ppc64le"), pin(name="Bad Name"), {**pin(), "extra": 1}):
            with self.assertRaises(ImporterError, msg=str(bad)):
                rpm_mirror.load_pins(self.write([bad]))

    def test_a_package_listed_twice_is_refused(self):
        with self.assertRaises(ImporterError):
            rpm_mirror.load_pins(self.write([pin(), pin()]))


class DownloadTests(unittest.TestCase):
    def test_a_package_that_does_not_match_its_pin_is_refused(self):
        with tempfile.TemporaryDirectory() as work, self.assertRaisesRegex(ImporterError, "SHA-256"):
            rpm_mirror.fetch_packages(work, [pin()], fetcher=lambda url: b"other")

    def test_a_matching_package_is_written_and_its_header_checked(self):
        with tempfile.TemporaryDirectory() as work, mock.patch.object(rpm_mirror, "inspect") as inspect:
            result = rpm_mirror.fetch_packages(work, [pin()], fetcher=lambda url: DATA)
            inspect.assert_called_once()
            self.assertEqual(Path(work, "tool-1.0.0-1.x86_64.rpm").read_bytes(), DATA)
            self.assertIn("tool-1.0.0-1.x86_64.rpm", result)

    def test_downloads_only_follow_redirects_to_allowed_hosts(self):
        class Response:
            status = 200
            headers = {"Content-Length": str(len(DATA))}

            def read(self, limit):
                return DATA

            def close(self):
                pass

        class Opener:
            def __init__(self, location):
                self.location = location
                self.opened = []

            def open(self, request, timeout):
                self.opened.append(request.full_url)
                if len(self.opened) == 1:
                    from urllib.error import HTTPError
                    raise HTTPError(request.full_url, 302, "found", {"Location": self.location}, None)
                return Response()

        good = Opener("https://packages.redhat.com/api/x.rpm")
        self.assertEqual(rpm_mirror.download(pin()["url"], opener=good), DATA)
        bad = Opener("https://evil.example/x.rpm")
        with self.assertRaisesRegex(ImporterError, "allowlist"):
            rpm_mirror.download(pin()["url"], opener=bad)
        with self.assertRaisesRegex(ImporterError, "allowlist"):
            rpm_mirror.download("https://evil.example/x.rpm", opener=good)


class RedirectChainTests(unittest.TestCase):
    def test_the_copr_chain_through_the_red_hat_mirror_to_its_bucket_is_followed(self):
        from urllib.error import HTTPError

        class Response:
            status = 200
            headers = {"Content-Length": str(len(DATA))}

            def read(self, limit):
                return DATA

            def close(self):
                pass

        chain = iter(["https://packages.redhat.com/api/x.rpm", "https://copr-pulp-prod.s3.amazonaws.com/a?sig=1"])

        class Opener:
            def open(self, request, timeout):
                location = next(chain, None)
                if location is None:
                    return Response()
                raise HTTPError(request.full_url, 302, "found", {"Location": location}, None)

        url = "https://download.copr.fedorainfracloud.org/results/x/x/fedora-45-x86_64/Packages/x/x.rpm"
        self.assertEqual(rpm_mirror.download(url, opener=Opener()), DATA)


class MergeTests(unittest.TestCase):
    def test_a_new_package_is_added_next_to_the_existing_ones(self):
        sirius = {"sirius-installer-1.0.5-1.x86_64.rpm": {"version": "1.0.5", "release_sha256": "a" * 64,
                                                          "sha256": "b" * 64, "source_run_id": "1",
                                                          "manifest_sha256": "c" * 64}}
        mirrored = {"x86_64": {"sirius-installer-1.0.5-1.x86_64.rpm": b"s"}, "aarch64": {}}
        files, ledger, new = rpm_mirror.merge_mirror(mirrored, {"x86_64": sirius, "aarch64": {}},
                                                     {"tool-1.0.0-1.x86_64.rpm": package(pin())})
        self.assertEqual(new, {"x86_64": ["tool-1.0.0-1.x86_64.rpm"]})
        self.assertIn("sirius-installer-1.0.5-1.x86_64.rpm", ledger["x86_64"])
        entry = ledger["x86_64"]["tool-1.0.0-1.x86_64.rpm"]
        self.assertEqual((entry["version"], entry["source_run_id"]), ("1.0.0-1", "mirror"))
        self.assertEqual(files["x86_64"]["tool-1.0.0-1.x86_64.rpm"], DATA)
        self.assertEqual(ledger["aarch64"], {})

    def test_a_noarch_package_goes_to_every_architecture(self):
        entry = pin(arch="noarch", url=pin()["url"].replace("x86_64", "noarch"))
        _files, ledger, new = rpm_mirror.merge_mirror({"x86_64": {}, "aarch64": {}}, {},
                                                      {"tool-1.0.0-1.noarch.rpm": package(entry)})
        self.assertEqual(set(new), {"x86_64", "aarch64"})
        self.assertIn("tool-1.0.0-1.noarch.rpm", ledger["aarch64"])

    def test_publishing_the_same_package_again_changes_nothing(self):
        first = rpm_mirror.merge_mirror({"x86_64": {}}, {}, {"tool-1.0.0-1.x86_64.rpm": package(pin())})
        again = rpm_mirror.merge_mirror(first[0], first[1], {"tool-1.0.0-1.x86_64.rpm": package(pin())})
        self.assertEqual(again[2], {})

    def test_the_same_file_with_other_content_is_refused(self):
        first = rpm_mirror.merge_mirror({"x86_64": {}}, {}, {"tool-1.0.0-1.x86_64.rpm": package(pin())})
        with self.assertRaisesRegex(ImporterError, "different content"):
            rpm_mirror.merge_mirror(first[0], first[1], {"tool-1.0.0-1.x86_64.rpm": package(pin(), b"other")})

    def test_versions_are_compared_per_package_and_must_increase(self):
        first = rpm_mirror.merge_mirror({"x86_64": {}}, {}, {"tool-2.0.0-1.x86_64.rpm":
                                                              package(pin(version="2.0.0"))})
        with self.assertRaisesRegex(ImporterError, "newer or equal"):
            rpm_mirror.merge_mirror(first[0], first[1], {"tool-1.0.0-1.x86_64.rpm": package(pin())})
        other = pin(name="other", url=pin()["url"].replace("tool", "other"))
        merged = rpm_mirror.merge_mirror(first[0], first[1], {"other-1.0.0-1.x86_64.rpm": package(other)})
        self.assertEqual(merged[2], {"x86_64": ["other-1.0.0-1.x86_64.rpm"]})
        rebuilt = pin(version="2.0.0", release="2")
        again = rpm_mirror.merge_mirror(first[0], first[1], {"tool-2.0.0-2.x86_64.rpm": package(rebuilt)})
        self.assertEqual(again[2], {"x86_64": ["tool-2.0.0-2.x86_64.rpm"]})

    def test_the_sirius_importer_ignores_other_packages_when_it_orders_versions(self):
        ledger = {"x86_64": {"tool-9.9.9-1.x86_64.rpm": {"version": "9.9.9-1", "release_sha256": "a" * 64,
                                                         "sha256": "a" * 64, "source_run_id": "mirror",
                                                         "manifest_sha256": "a" * 64}},
                  "aarch64": {}}
        checked = {"version": "1.0.6", "manifest_sha256": "d" * 64, "source_run_id": "7"}
        with tempfile.TemporaryDirectory() as directory:
            paths = {arch: Path(directory) / f"sirius-installer-1.0.6-1.{arch}.rpm" for arch in rpm_import.ARCHITECTURES}
            for path in paths.values():
                path.write_bytes(b"x")
            inspected = {arch: {"filename": paths[arch].name, "sha256": "e" * 64} for arch in paths}
            _files, updated, new = rpm_import.merge_release({"x86_64": {}, "aarch64": {}}, ledger, checked,
                                                            paths, inspected)
        self.assertEqual(set(new), {"x86_64", "aarch64"})
        self.assertIn("tool-9.9.9-1.x86_64.rpm", updated["x86_64"])


class MirrorIdentityTests(unittest.TestCase):
    def descriptor(self, **changes):
        value = {"format": "rpm", "channel": "testing", "package_id": issuer.MIRROR_PACKAGE_ID,
                 "version": issuer.MIRROR_VERSION, "git_sha": "a" * 40, "source_run_id": "123",
                 "source_run_attempt": 1}
        return {**value, **changes}

    ENV = {"GITHUB_REPOSITORY": "luminusOS/rpm-repo", "GITHUB_REF": "refs/heads/main",
           "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}

    def test_the_run_of_this_repository_on_main_is_accepted(self):
        self.assertEqual(issuer.verify_mirror_run(self.descriptor(), "testing", self.ENV)["package_id"],
                         issuer.MIRROR_PACKAGE_ID)

    def test_any_other_run_is_refused(self):
        for key, value in (("GITHUB_REPOSITORY", "someone/else"), ("GITHUB_REF", "refs/heads/feature"),
                           ("GITHUB_SHA", "b" * 40), ("GITHUB_RUN_ID", "124"), ("GITHUB_RUN_ATTEMPT", "2")):
            with self.assertRaises(issuer.IssuerError, msg=key):
                issuer.verify_mirror_run(self.descriptor(), "testing", {**self.ENV, key: value})

    def test_a_malformed_mirror_descriptor_is_refused(self):
        for changes in ({"version": "1.0.0"}, {"git_sha": "zz"}, {"source_run_id": "x"},
                        {"source_run_attempt": 0}, {"channel": "stable"}):
            with self.assertRaises(issuer.IssuerError, msg=str(changes)):
                issuer.verify_mirror_run(self.descriptor(**changes), "testing", self.ENV)

    def test_the_mirror_is_not_a_source_release_package(self):
        self.assertNotIn(issuer.MIRROR_PACKAGE_ID, issuer.SOURCE_RELEASES)


if __name__ == "__main__":
    unittest.main()
