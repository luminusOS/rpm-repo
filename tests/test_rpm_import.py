import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import rpm_import  # noqa: E402
from rpm_import import ImporterError  # noqa: E402

TOOLS = ("rpm", "rpmbuild", "rpmsign", "createrepo_c", "gpg", "rpmkeys")
HAVE_TOOLS = all(shutil.which(tool) for tool in TOOLS)
FIXTURE_ROOT = "/tmp/opencode" if os.path.isdir("/tmp/opencode") else None
RUN = {"source_run_id": "35161979157", "source_run_attempt": 1, "git_sha": "a" * 40}


def descriptor(version="1.0.5-rc.1", channel="testing"):
    tag = "v" + version
    return {"format": "rpm", "channel": channel, "package_id": "sirius-installer", "version": version,
            "tag": tag, **RUN}


def run(*command, cwd=None, env=None, input_data=None):
    result = subprocess.run(command, cwd=cwd, env=env, input=input_data, capture_output=True, timeout=300)
    if result.returncode:
        raise AssertionError(f"{' '.join(map(str, command))}: {result.stderr.decode()}")
    return result.stdout


class DescriptorTests(unittest.TestCase):
    def test_a_valid_descriptor_names_both_architecture_packages(self):
        plan = rpm_import.fetch_plan(descriptor())
        self.assertEqual(plan.repository, "luminusOS/sirius")
        self.assertTrue(plan.asset_urls["aarch64"].endswith("/v1.0.5-rc.1/sirius-installer-1.0.5-rc.1-1.aarch64.rpm"))
        self.assertTrue(plan.checksums_url.endswith("/v1.0.5-rc.1/checksums.txt"))

    def test_pre_releases_cannot_enter_the_stable_channel(self):
        with self.assertRaises(ImporterError):
            rpm_import.validate_descriptor(descriptor("1.0.5-rc.1", "stable"))

    def test_tag_and_version_must_agree_and_other_packages_are_refused(self):
        with self.assertRaises(ImporterError):
            rpm_import.validate_descriptor({**descriptor(), "tag": "v1.0.6-rc.1"})
        with self.assertRaises(ImporterError):
            rpm_import.validate_descriptor({**descriptor(), "package_id": "dev.luminusos.ZapTide"})
        with self.assertRaises(ImporterError):
            rpm_import.validate_descriptor({**descriptor(), "extra": 1})

    def test_rpm_versions_use_the_tilde_form(self):
        self.assertEqual(rpm_import.rpm_version("1.0.5-rc.1"), "1.0.5~rc.1")
        self.assertEqual(rpm_import.rpm_version("1.0.5"), "1.0.5")

    def test_checksums_must_be_exact(self):
        good = ("a" * 64 + "  sirius-installer-1.0.5-1.x86_64.rpm\n").encode()
        self.assertEqual(len(rpm_import.parse_checksums(good)), 1)
        for bad in (b"", b"abc  file\n", good + good, ("a" * 64 + "  ../x.rpm\n").encode()):
            with self.assertRaises(ImporterError):
                rpm_import.parse_checksums(bad)

    def test_location_and_key_paths_cannot_escape(self):
        for bad in ("../x.rpm", "Packages/../x.rpm", "Packages/a/b.rpm", "/Packages/x.rpm", "x.rpm/"):
            self.assertIsNone(rpm_import.RPM_NAME.fullmatch(bad))
        with self.assertRaises(ImporterError):
            rpm_import._checked_key("testing", "testing/../stable/x")
        with self.assertRaises(ImporterError):
            rpm_import._checked_key("testing", "stable/x")


class UploadOrderTests(unittest.TestCase):
    def test_packages_are_written_before_metadata_and_metadata_ends_with_the_signature_and_ledger(self):
        stage = Path(tempfile.mkdtemp(dir=FIXTURE_ROOT))
        self.addCleanup(shutil.rmtree, stage)
        for name in ("45/x86_64/Packages/a.rpm", "45/x86_64/repodata/abc-primary.xml.gz",
                     "45/x86_64/repodata/repomd.xml", "45/x86_64/repodata/repomd.xml.asc",
                     "luminus-imports.json"):
            (stage / name).parent.mkdir(parents=True, exist_ok=True)
            (stage / name).write_bytes(b"x")
        plan = rpm_import.plan_uploads(stage, "testing", {}, {"luminus.asc": b"k"})
        keys = [item[1] for item in plan]
        self.assertEqual(keys[0], "testing/45/x86_64/Packages/a.rpm")
        self.assertLess(keys.index("testing/45/x86_64/repodata/abc-primary.xml.gz"),
                        keys.index("testing/45/x86_64/repodata/repomd.xml"))
        self.assertLess(keys.index("testing/45/x86_64/repodata/repomd.xml"),
                        keys.index("testing/45/x86_64/repodata/repomd.xml.asc"))
        self.assertLess(keys.index("testing/45/x86_64/repodata/repomd.xml.asc"),
                        keys.index("testing/luminus-imports.json"))

    def test_an_existing_package_of_another_size_is_rejected(self):
        stage = Path(tempfile.mkdtemp(dir=FIXTURE_ROOT))
        self.addCleanup(shutil.rmtree, stage)
        (stage / "45/x86_64/Packages").mkdir(parents=True)
        (stage / "45/x86_64/Packages/a.rpm").write_bytes(b"abc")
        with self.assertRaises(ImporterError):
            rpm_import.plan_uploads(stage, "testing", {"testing/45/x86_64/Packages/a.rpm": 99})

    def test_expired_credentials_stop_before_any_metadata_is_written(self):
        written = []

        class Storage:
            def put(self, key, content):
                written.append(key)

        now = [100.0]

        def clock():
            now[0] += 60
            return now[0]

        plan = [(0, "testing/45/x86_64/Packages/a.rpm", b"a"), (0, "testing/45/aarch64/Packages/a.rpm", b"a"),
                (2, "testing/45/x86_64/repodata/repomd.xml", b"m")]
        with self.assertRaises(ImporterError):
            rpm_import.upload(plan, "testing", {"expires_at": 200.0}, storage=Storage(),
                              public_verify=lambda key, content: True, clock=clock, mutable_budget=0)
        self.assertNotIn("testing/45/x86_64/repodata/repomd.xml", written)

    def test_a_failed_public_check_of_a_package_stops_the_publication(self):
        written = []

        class Storage:
            def put(self, key, content):
                written.append(key)

        plan = [(0, "testing/45/x86_64/Packages/a.rpm", b"a"), (2, "testing/45/x86_64/repodata/repomd.xml", b"m")]
        with self.assertRaises(ImporterError):
            rpm_import.upload(plan, "testing", {"expires_at": 10 ** 12}, storage=Storage(),
                              public_verify=lambda key, content: False)
        self.assertEqual(written, ["testing/45/x86_64/Packages/a.rpm"])


@unittest.skipUnless(HAVE_TOOLS, "rpm, rpmbuild, rpmsign, createrepo_c and gpg are required")
class RealRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="rpm-import-", dir=FIXTURE_ROOT)
        cls.base = Path(cls.temp.name)
        cls.gnupg = cls.base / "gnupg"
        cls.gnupg.mkdir(mode=0o700)
        cls.env = {**os.environ, "GNUPGHOME": str(cls.gnupg), "HOME": str(cls.base)}
        cls.fingerprints = {}
        cls.root = cls.base / "root"
        (cls.root / "trust").mkdir(parents=True)
        channels = {}
        for channel in ("stable", "testing"):
            primary, subkey = cls.make_key(channel)
            channels[channel] = {"primary_fingerprint": primary, "signing_subkey_fingerprint": subkey,
                                 "public_key": f"trust/{channel}.asc"}
            (cls.root / f"trust/{channel}.asc").write_bytes(run("gpg", "--armor", "--export", primary, env=cls.env))
            cls.fingerprints[channel] = subkey
        (cls.root / "trust.json").write_text(json.dumps(
            {"schema_version": 1, "format": "rpm", "status": "provisioned", "max_validity_days": 400,
             "channels": channels}))
        cls.packages = {}

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", str(cls.gnupg), "--kill", "gpg-agent"], capture_output=True)
        cls.temp.cleanup()

    @classmethod
    def make_key(cls, name):
        run("gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key",
            f"{name} <{name}@example.invalid>", "rsa3072", "cert", "never", env=cls.env)
        listing = run("gpg", "--list-keys", "--with-colons", f"{name}@example.invalid", env=cls.env).decode()
        primary = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr"))
        run("gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "", "--quick-add-key", primary,
            "rsa3072", "sign", "never", env=cls.env)
        listing = run("gpg", "--list-keys", "--with-colons", primary, env=cls.env).decode().splitlines()
        subkey = None
        for index, line in enumerate(listing):
            if line.startswith("sub:"):
                subkey = listing[index + 1].split(":")[9]
        return primary, subkey

    @classmethod
    def build_rpm(cls, version, arch, extra_files=()):
        key = (version, arch, tuple(extra_files))
        if key in cls.packages:
            return cls.packages[key]
        top = Path(tempfile.mkdtemp(prefix="rpmbuild-", dir=cls.base))
        (top / "SPECS").mkdir()
        files = ["/usr/bin/sirius", *extra_files]
        install = "\n".join(f"mkdir -p %{{buildroot}}$(dirname {name}); echo x > %{{buildroot}}{name}" for name in files)
        listing = "\n".join(f"{name}" for name in files)
        spec = "\n".join([
            "Name: sirius-installer", f"Version: {rpm_import.rpm_version(version)}", "Release: 1", "Summary: Fixture",
            "License: MIT", "AutoReqProv: no", "%description", "Fixture package.", "%install", install,
            "%files", listing, ""])
        (top / "SPECS/f.spec").write_text(spec)
        run("rpmbuild", "-bb", "--target", arch, "--define", f"_topdir {top}", str(top / "SPECS/f.spec"),
            env=cls.env)
        built = next((top / "RPMS").rglob("*.rpm"))
        data = built.read_bytes()
        cls.packages[key] = data
        return data

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.base))
        self.storage = {}
        self.assets = {}
        self.public_calls = []

    def release(self, version, channel="testing", *, tamper=None, extra_files=()):
        """Serve one fake source release and return its descriptor."""
        built = {arch: self.build_rpm(version, arch, extra_files) for arch in rpm_import.ARCHITECTURES}
        if tamper == "header":
            built["aarch64"] = self.build_rpm(version, "x86_64", extra_files)
        names = {arch: rpm_import.rpm_filename(version, arch) for arch in built}
        base = f"https://github.com/luminusOS/sirius/releases/download/v{version}"
        self.assets = {f"{base}/{names[arch]}": built[arch] for arch in built}
        digests = {names[arch]: hashlib.sha256(built[arch]).hexdigest() for arch in built}
        if tamper == "checksum":
            digests[names["x86_64"]] = "0" * 64
        self.assets[f"{base}/checksums.txt"] = "".join(f"{value}  {name}\n" for name, value in digests.items()).encode()
        return descriptor(version, channel)

    def fetch(self):
        return mock.patch.object(rpm_import, "download_asset",
                                 lambda url, limit, opener=None: self.assets[url])

    def public(self):
        storage = self.storage

        def getter(url, limit):
            key = url.removeprefix(rpm_import.PUBLIC_BASE + "/")
            return storage.get(key)

        return rpm_import._Public(getter)

    def storage_object(self):
        outer = self

        class Storage:
            def put(self, key, content):
                outer.storage[key] = bytes(content)

            def list_keys(self, prefix):
                return {key: len(value) for key, value in outer.storage.items() if key.startswith(prefix)}

        return Storage()

    def publish(self, desc, **kwargs):
        credentials = self.work / "credentials.json"
        credentials.write_text("{}")
        channel = desc["channel"]
        lister = lambda repository, run_id, fetcher=None: {"x86_64": 11, "aarch64": 12}  # noqa: E731
        with self.fetch(), mock.patch.object(rpm_import.r2_probe, "load_credentials", lambda path: {
                "bucket": rpm_import.issuer.BUCKET, "prefix": channel + "/", "expires_at": 10 ** 12}), \
                mock.patch.object(rpm_import, "verify_tmpfs", lambda path: Path(path)):
            return rpm_import.publish_release(
                desc, self.work / f"run-{len(list(self.work.iterdir()))}", credentials, self.gnupg,
                self.fingerprints[channel], root=self.root, public=self.public(), storage=self.storage_object(),
                public_verify=lambda key, content: self.storage.get(key) == content, artifact_lister=lister, **kwargs)

    def prepare(self, desc):
        lister = lambda repository, run_id, fetcher=None: {"x86_64": 11, "aarch64": 12}  # noqa: E731
        with self.fetch():
            return rpm_import.prepare_release(desc, self.work / "prepare", root=self.root, public=self.public(),
                                              artifact_lister=lister)

    def checksig(self, data, channel):
        db = self.work / "rpmdb"
        db.mkdir(exist_ok=True)
        key = self.root / f"trust/{channel}.asc"
        run("rpmkeys", "--dbpath", str(db), "--import", str(key))
        package = self.work / "check.rpm"
        package.write_bytes(data)
        result = subprocess.run(["rpmkeys", "--dbpath", str(db), "--checksig", str(package)], capture_output=True, text=True)
        return result.returncode == 0 and "signatures OK" in result.stdout

    def test_first_publication_creates_signed_packages_and_metadata_for_both_architectures(self):
        result = self.publish(self.release("1.0.5-rc.1"))
        self.assertEqual(result["status"], "imported")
        for arch in rpm_import.ARCHITECTURES:
            name = f"testing/45/{arch}/Packages/sirius-installer-1.0.5-rc.1-1.{arch}.rpm"
            self.assertIn(name, self.storage)
            self.assertTrue(self.checksig(self.storage[name], "testing"), arch)
            self.assertFalse(self.checksig(self.build_rpm("1.0.5-rc.1", arch), "testing"), "unsigned package accepted")
            self.assertIn(f"testing/45/{arch}/repodata/repomd.xml", self.storage)
            self.assertIn(f"testing/45/{arch}/repodata/repomd.xml.asc", self.storage)
        self.assertIn("testing/luminus-imports.json", self.storage)
        self.assertIn("testing/luminus.asc", self.storage)
        self.assertFalse(any(key.startswith("stable/") for key in self.storage))

    def test_a_signed_package_is_not_accepted_with_the_other_channel_key(self):
        self.publish(self.release("1.0.5-rc.1"))
        key = "testing/45/x86_64/Packages/sirius-installer-1.0.5-rc.1-1.x86_64.rpm"
        self.assertFalse(self.checksig(self.storage[key], "stable"))

    def test_a_later_version_accumulates_and_both_versions_stay_listed(self):
        self.publish(self.release("1.0.5-rc.1"))
        result = self.publish(self.release("1.0.5-rc.2"))
        self.assertEqual(result["status"], "imported")
        ledger = json.loads(json.loads(self.storage["testing/luminus-imports.json"])["ledger"])
        for arch in rpm_import.ARCHITECTURES:
            self.assertEqual(len(ledger["packages"][arch]), 2)
            self.assertEqual(sum(1 for key in self.storage if key.startswith(f"testing/45/{arch}/Packages/")), 2)

    def test_running_the_same_release_again_changes_nothing(self):
        desc = self.release("1.0.5-rc.1")
        self.publish(desc)
        before = dict(self.storage)
        self.assertEqual(self.publish(desc)["status"], "unchanged")
        self.assertEqual(self.storage, before)
        self.assertEqual(self.prepare(desc)["status"], "unchanged")

    def test_older_versions_and_changed_content_are_rejected(self):
        self.publish(self.release("1.0.5-rc.2"))
        with self.assertRaises(ImporterError):
            self.publish(self.release("1.0.5-rc.1"))
        with self.assertRaises(ImporterError):
            self.publish(self.release("1.0.5-rc.2", extra_files=("/usr/share/sirius/extra",)))

    def test_release_assets_that_do_not_match_are_rejected_before_anything_is_written(self):
        for tamper in ("checksum", "header"):
            with self.subTest(tamper):
                with self.assertRaises(ImporterError):
                    self.publish(self.release("1.0.5-rc.1", tamper=tamper))
                self.assertEqual(self.storage, {})

    def test_a_package_that_ships_the_distribution_descriptor_is_rejected(self):
        with self.assertRaises(ImporterError):
            self.publish(self.release("1.0.5-rc.1", extra_files=("/etc/sirius/distro.toml",)))
        with self.assertRaises(ImporterError):
            self.publish(self.release("1.0.5-rc.1", extra_files=("/etc/polkit-1/rules.d/50-live.rules",)))
        self.assertEqual(self.storage, {})

    def test_a_tampered_public_repository_stops_the_import(self):
        self.publish(self.release("1.0.5-rc.1"))
        pristine = dict(self.storage)
        next_release = self.release("1.0.5-rc.2")
        cases = {
            "package": lambda s: s.__setitem__(
                "testing/45/aarch64/Packages/sirius-installer-1.0.5-rc.1-1.aarch64.rpm", b"evil"),
            "ledger": lambda s: s.__setitem__("testing/luminus-imports.json", json.dumps(
                {**json.loads(s["testing/luminus-imports.json"]),
                 "ledger": json.loads(s["testing/luminus-imports.json"])["ledger"] + " "}).encode()),
            "ledger signature replaced": lambda s: s.__setitem__("testing/luminus-imports.json", json.dumps(
                {**json.loads(s["testing/luminus-imports.json"]), "signature": ""}).encode()),
            "package removed": lambda s: s.pop(
                "testing/45/x86_64/Packages/sirius-installer-1.0.5-rc.1-1.x86_64.rpm"),
        }
        for name, damage in cases.items():
            with self.subTest(name):
                self.storage.clear()
                self.storage.update(pristine)
                damage(self.storage)
                damaged = dict(self.storage)
                with self.assertRaises(ImporterError):
                    self.publish(next_release)
                self.assertEqual(self.storage, damaged)

    def test_a_channel_with_the_earlier_detached_signature_ledger_can_still_be_extended(self):
        self.publish(self.release("1.0.5-rc.1"))
        envelope = json.loads(self.storage["testing/luminus-imports.json"])
        self.storage["testing/luminus-imports.json"] = envelope["ledger"].encode()
        self.storage["testing/luminus-imports.json.sig"] = envelope["signature"].encode()
        self.assertEqual(self.publish(self.release("1.0.5-rc.2"))["status"], "imported")
        self.assertEqual(set(json.loads(self.storage["testing/luminus-imports.json"])), {"ledger", "signature"})
        self.assertIn("testing/luminus-imports.json.sig", self.storage)
        self.assertEqual(self.publish(self.release("1.0.5-rc.3"))["status"], "imported")
        self.storage["testing/luminus-imports.json"] = envelope["ledger"].encode() + b" "
        with self.assertRaises(ImporterError):
            self.publish(self.release("1.0.5-rc.4"))

    def test_damaged_repository_metadata_is_rebuilt_from_the_verified_packages(self):
        self.publish(self.release("1.0.5-rc.1"))
        pristine = dict(self.storage)
        cases = {
            "repomd changed": lambda s: s.__setitem__("testing/45/x86_64/repodata/repomd.xml",
                                                      s["testing/45/x86_64/repodata/repomd.xml"] + b" "),
            "signature removed": lambda s: s.pop("testing/45/x86_64/repodata/repomd.xml.asc"),
            "metadata removed": lambda s: [s.pop(k) for k in [k for k in s if "/repodata/" in k]],
        }
        for name, damage in cases.items():
            with self.subTest(name):
                self.storage.clear()
                self.storage.update(pristine)
                damage(self.storage)
                self.assertEqual(self.publish(self.release("1.0.5-rc.2"))["status"], "imported")
                for arch in rpm_import.ARCHITECTURES:
                    self.assertEqual(self.verify_metadata(arch), "ok", name)

    def verify_metadata(self, arch):
        repomd = self.storage[f"testing/45/{arch}/repodata/repomd.xml"]
        signature = self.storage[f"testing/45/{arch}/repodata/repomd.xml.asc"]
        rpm_import._verify_detached(self.root, "testing", repomd, signature, self.work)
        return "ok"

    def interrupted(self, release, fail_on):
        outer = self
        storage = self.storage_object()
        count = {"n": 0}

        class Interrupted:
            def put(self, key, content):
                count["n"] += 1
                if fail_on(key, count["n"]):
                    raise ImporterError("simulated interruption")
                storage.put(key, content)

            def list_keys(self, prefix):
                return storage.list_keys(prefix)

        credentials = self.work / "credentials.json"
        credentials.write_text("{}")
        channel = release["channel"]
        lister = lambda repository, run_id, fetcher=None: {"x86_64": 11, "aarch64": 12}  # noqa: E731
        with self.fetch(), mock.patch.object(rpm_import.r2_probe, "load_credentials", lambda path: {
                "bucket": rpm_import.issuer.BUCKET, "prefix": channel + "/", "expires_at": 10 ** 12}), \
                mock.patch.object(rpm_import, "verify_tmpfs", lambda path: Path(path)):
            return rpm_import.publish_release(
                release, self.work / f"cut-{count['n']}-{outer.work.stat().st_nlink}", credentials, self.gnupg,
                self.fingerprints[channel], root=self.root, public=self.public(), storage=Interrupted(),
                public_verify=lambda key, content: self.storage.get(key) == content, artifact_lister=lister)

    def test_a_run_cut_off_at_any_upload_can_be_repeated_and_leaves_a_consistent_channel(self):
        self.publish(self.release("1.0.5-rc.1"))
        base = dict(self.storage)
        release = self.release("1.0.5-rc.2")
        total = len(self.interrupted_plan(release))
        for cut in range(1, total + 1):
            with self.subTest(cut=cut):
                self.storage.clear()
                self.storage.update(base)
                with self.assertRaises(ImporterError):
                    self.interrupted(release, lambda key, n, cut=cut: n == cut)
                result = self.publish(release)
                self.assertIn(result["status"], ("imported", "unchanged"))
                ledger = json.loads(json.loads(self.storage["testing/luminus-imports.json"])["ledger"])["packages"]
                for arch in rpm_import.ARCHITECTURES:
                    self.assertEqual(len(ledger[arch]), 2)
                    self.assertEqual(self.verify_metadata(arch), "ok")
                    for name, entry in ledger[arch].items():
                        data = self.storage[f"testing/45/{arch}/Packages/{name}"]
                        self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
                        self.assertTrue(self.checksig(data, "testing"))

    def interrupted_plan(self, release):
        sizes = []
        self.interrupted(release, lambda key, n: sizes.append(n) or False)
        count = len(sizes)
        self.storage.clear()
        return range(count)

    def test_signing_with_the_other_channels_key_is_refused_before_anything_is_written(self):
        desc = self.release("1.0.5-rc.1")
        credentials = self.work / "credentials.json"
        credentials.write_text("{}")
        with self.fetch(), mock.patch.object(rpm_import, "verify_tmpfs", lambda path: Path(path)):
            with self.assertRaises(ImporterError):
                rpm_import.publish_release(
                    desc, self.work / "swap", credentials, self.gnupg, self.fingerprints["stable"], root=self.root,
                    public=self.public(), storage=self.storage_object(),
                    artifact_lister=lambda repository, run_id, fetcher=None: {"x86_64": 11, "aarch64": 12})
        self.assertEqual(self.storage, {})

    def test_the_stable_channel_refuses_a_release_candidate(self):
        with self.assertRaises(ImporterError):
            self.publish(self.release("1.0.5-rc.1", "stable"))

    @unittest.skipUnless(shutil.which("dnf5") or shutil.which("dnf"), "dnf is required")
    def test_dnf_installs_from_the_repository_only_with_valid_signatures(self):
        self.publish(self.release("1.0.5-rc.1"))
        host = subprocess.run(["rpm", "--eval", "%{_arch}"], capture_output=True, text=True).stdout.strip()
        served = self.work / "served"
        for key, value in self.storage.items():
            target = served / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        repo = served / "testing/45" / host
        dnf = shutil.which("dnf5") or shutil.which("dnf")
        key_file = served / "testing/luminus.asc"

        def attempt(name, *, install_root):
            command = [dnf, "install", "-y", "--installroot", str(install_root), "--releasever", "45",
                       "--disablerepo=*", "--repofrompath", f"luminus,file://{repo}",
                       "--setopt=luminus.gpgcheck=1", "--setopt=luminus.repo_gpgcheck=1",
                       f"--setopt=luminus.gpgkey=file://{key_file}", "--setopt=install_weak_deps=0",
                       "--use-host-config" if "dnf5" in dnf else "--noplugins", "sirius-installer"]
            command = [item for item in command if item != "--use-host-config"]
            return subprocess.run(command, capture_output=True, text=True, timeout=300)

        good = attempt("good", install_root=self.work / "root-good")
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertTrue((self.work / "root-good/usr/bin/sirius").exists())

        package = next((repo / "Packages").glob("*.rpm"))
        original = package.read_bytes()
        package.write_bytes(original[:-4] + b"evil")
        self.assertNotEqual(attempt("tampered", install_root=self.work / "root-bad").returncode, 0)
        package.write_bytes(original)
        (repo / "repodata/repomd.xml.asc").write_bytes(b"")
        self.assertNotEqual(attempt("unsigned repo", install_root=self.work / "root-bad2").returncode, 0)
        self.assertFalse((self.work / "root-bad/usr/bin/sirius").exists())


if __name__ == "__main__":
    unittest.main()
