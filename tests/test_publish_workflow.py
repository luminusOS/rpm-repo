import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"duplicate workflow key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


class PublishWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        cls.workflow = yaml.load(cls.raw, Loader=UniqueKeyLoader)

    def test_only_dispatch_runs_on_protected_main_with_read_permissions(self):
        self.assertEqual(set(self.workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        for job in self.workflow["jobs"].values():
            self.assertIn("github.ref_protected", job["if"])
            self.assertIn("refs/heads/main", job["if"])

    def test_jobs_are_gated_in_checks_issuer_publisher_order(self):
        self.assertEqual(list(self.workflow["jobs"]), ["checks", "prepare", "issuer", "publisher"])
        self.assertEqual(set(self.workflow["jobs"]), {"checks", "prepare", "issuer", "publisher"})
        self.assertEqual(self.workflow["jobs"]["prepare"]["needs"], "checks")
        self.assertEqual(self.workflow["jobs"]["issuer"]["needs"], "prepare")
        self.assertEqual(self.workflow["jobs"]["publisher"]["needs"], "issuer")
        self.assertNotIn("environment", self.workflow["jobs"]["prepare"])
        self.assertIn("issuer-${{ inputs.channel }}", self.workflow["jobs"]["issuer"]["environment"])
        self.assertIn("publisher-${{ inputs.channel }}", self.workflow["jobs"]["publisher"]["environment"])

    def key_check_script(self):
        publisher = self.workflow["jobs"]["publisher"]
        run = next(step["run"] for step in publisher["steps"]
                   if step.get("name", "").startswith("Import signing subkey"))
        marker = "python3 - <<'PY'\n"
        start = run.index(marker) + len(marker)
        return run[start:run.index("\nPY", start)]

    def subkey_only_home(self, root):
        source, target = root / "source", root / "target"
        for home in (source, target):
            home.mkdir(mode=0o700)
        env = {**os.environ, "GNUPGHOME": str(source), "LC_ALL": "C"}
        run = lambda *args, **kw: subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback",
                                                 "--passphrase", "", *args], env=env, check=True,
                                                capture_output=True, text=True, **kw)
        run("--quick-generate-key", "Fixture <f@example.invalid>", "rsa2048", "cert", "1d")
        primary = next(line.split(":")[9] for line in run("--with-colons", "--list-keys").stdout.splitlines()
                       if line.startswith("fpr:"))
        run("--quick-add-key", primary, "rsa2048", "sign", "1d")
        fingerprints = [line.split(":")[9] for line in
                        run("--with-colons", "--fingerprint", "--fingerprint", "--list-keys").stdout.splitlines()
                        if line.startswith("fpr:")]
        exported = subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                                   "--armor", "--export-secret-subkeys", fingerprints[1] + "!"],
                                  env=env, check=True, capture_output=True).stdout
        subprocess.run(["gpgconf", "--homedir", str(source), "--kill", "gpg-agent"], capture_output=True)
        subprocess.run(["gpg", "--batch", "--import"], env={**env, "GNUPGHOME": str(target)}, input=exported,
                       check=True, capture_output=True)
        return target, fingerprints[0], fingerprints[1]

    def test_key_material_check_accepts_a_subkey_only_export_and_rejects_others(self):
        script = self.key_check_script()
        with tempfile.TemporaryDirectory(prefix="key-check-", dir="/tmp") as directory:
            target, primary, subkey = self.subkey_only_home(Path(directory))
            env = {**os.environ, "GNUPGHOME": str(target), "LC_ALL": "C",
                   "SIGNING_PRIMARY_FINGERPRINT": primary, "SIGNING_SUBKEY_FINGERPRINT": subkey}
            ok = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            for name, wrong in (("SIGNING_PRIMARY_FINGERPRINT", "0" * 40), ("SIGNING_SUBKEY_FINGERPRINT", "1" * 40)):
                bad = subprocess.run([sys.executable, "-c", script], env={**env, name: wrong},
                                     capture_output=True, text=True)
                self.assertNotEqual(bad.returncode, 0)
            subprocess.run(["gpgconf", "--homedir", str(target), "--kill", "gpg-agent"], capture_output=True)

    def test_key_material_check_rejects_a_home_with_the_primary_secret(self):
        script = self.key_check_script()
        with tempfile.TemporaryDirectory(prefix="key-check-", dir="/tmp") as directory:
            home = Path(directory) / "full"
            home.mkdir(mode=0o700)
            env = {**os.environ, "GNUPGHOME": str(home), "LC_ALL": "C"}
            subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                            "--quick-generate-key", "Full <f@example.invalid>", "rsa2048", "cert", "1d"],
                           env=env, check=True, capture_output=True)
            listing = subprocess.run(["gpg", "--batch", "--with-colons", "--list-keys"], env=env,
                                     check=True, capture_output=True, text=True).stdout
            primary = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
            subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                            "--quick-add-key", primary, "rsa2048", "sign", "1d"], env=env,
                           check=True, capture_output=True)
            listing = subprocess.run(["gpg", "--batch", "--with-colons", "--fingerprint", "--fingerprint",
                                      "--list-keys"], env=env, check=True, capture_output=True, text=True).stdout
            subkey = [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")][1]
            bad = subprocess.run([sys.executable, "-c", script],
                                 env={**env, "SIGNING_PRIMARY_FINGERPRINT": primary,
                                      "SIGNING_SUBKEY_FINGERPRINT": subkey},
                                 capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], capture_output=True)

    def test_issuer_uses_maximum_supported_credential_ttl(self):
        issuer = self.workflow["jobs"]["issuer"]
        issue = next(step for step in issuer["steps"]
                     if step.get("name", "").startswith("Issue channel-scoped"))
        self.assertIn("--ttl 900", issue["run"])

    def test_credential_material_never_enters_runner_environment_or_later_steps(self):
        self.assertNotIn("GITHUB_ENV", self.raw)
        self.assertNotIn("GITHUB_OUTPUT", self.raw)
        steps = self.workflow["jobs"]["publisher"]["steps"]
        upload = next(i for i, step in enumerate(steps)
                      if step.get("name", "").startswith("Verify the release again"))
        for step in steps[upload + 1:]:
            self.assertNotIn("AWS_ACCESS_KEY_ID", str(step))
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", str(step))
            self.assertNotIn("AWS_SESSION_TOKEN", str(step))

    def test_signing_home_is_verified_tmpfs(self):
        self.assertIn("/dev/shm/luminus-publish-gnupg", self.raw)
        self.assertIn('stat -f -c %T "$GNUPGHOME"', self.raw)

    def test_concurrency_is_per_channel_and_not_cancelled(self):
        self.assertIn("${{ inputs.channel }}", self.workflow["concurrency"]["group"])
        self.assertIs(self.workflow["concurrency"]["cancel-in-progress"], False)

    def test_actions_are_pinned_and_run_scripts_do_not_interpolate_inputs(self):
        uses = re.findall(r"uses:\s*([^\s]+)", self.raw)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"@([0-9a-f]{40})$")
        for job in self.workflow["jobs"].values():
            for step in job.get("steps", []):
                if "run" in step:
                    self.assertNotIn("${{ inputs.", step["run"])


    def test_prepare_is_credential_free_and_capsule_consumption_is_the_first_publisher_step_that_uses_the_repository(self):
        prepare = self.workflow["jobs"]["prepare"]
        serialized = str(prepare)
        self.assertNotIn("secrets.", serialized)
        self.assertNotIn("R2_PARENT", serialized)
        steps = self.workflow["jobs"]["publisher"]["steps"]
        names = [step.get("name", "") for step in steps]
        install = next(i for i, name in enumerate(names) if name.startswith("Install signing"))
        checkout = next(i for i, step in enumerate(steps) if str(step.get("uses", "")).startswith("actions/checkout@"))
        venv = next(i for i, name in enumerate(names) if name.startswith("Prepare Python"))
        consume = names.index("Consume capsule and verify publisher context")
        self.assertLess(install, checkout)
        self.assertLess(checkout, venv)
        self.assertLess(checkout, consume)

    def test_every_job_that_runs_python_tools_in_a_container_installs_them_first(self):
        for name in ("checks", "prepare", "publisher"):
            job = self.workflow["jobs"][name]
            self.assertEqual(job["container"], "registry.fedoraproject.org/fedora:45")
            self.assertIn("dnf install", job["steps"][0]["run"])

    def test_the_publisher_signs_with_the_rpm_importer_and_has_no_flatpak_leftovers(self):
        self.assertIn("tools/rpm_import.py --mode publish", self.raw)
        self.assertIn("tools/rpm_import.py --mode prepare", self.raw)
        self.assertNotIn("flatpak", self.raw.lower())
        self.assertNotIn("ostree", self.raw.lower())
        self.assertIn('"format":"rpm"', self.raw)


if __name__ == "__main__":
    unittest.main()
