#!/usr/bin/env python3
"""Publishes the pinned packages of mirror.json into one channel of the DNF repository.

Some packages the images need are not built by a Luminus release workflow: they are
published by their upstream, or they are repackaged from a release asset. Each one is
pinned in mirror.json by exact URL and SHA-256, so changing what the repository serves
needs a reviewed commit to the protected branch. The importer downloads each package,
checks the pin and the RPM header, signs it with the channel key and merges it into
the channel next to the packages that are already there.
"""

import argparse
import hashlib
import hmac
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import issuer
import r2_probe
import rpm_import
from rpm_import import ARCHITECTURES, FEDORA, ImporterError

MIRROR_FILE = "mirror.json"
SOURCE_RUN = "mirror"
START_HOSTS = {"github.com", "download.copr.fedorainfracloud.org"}
REDIRECT_HOSTS = {"objects.githubusercontent.com", "release-assets.githubusercontent.com",
                  "packages.redhat.com", "copr-pulp-prod.s3.amazonaws.com"}
MAX_REDIRECTS = 3
_NAME = re.compile(r"[a-z0-9][a-z0-9+._-]{0,99}\Z")
_FIELD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~]{0,63}\Z")
_ENTRY_KEYS = {"name", "version", "release", "arch", "url", "sha256", "license"}


def load_pins(root=None):
    path = Path(root or Path(__file__).resolve().parents[1]) / MIRROR_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ImporterError("mirror list cannot be read") from error
    if (not isinstance(document, dict) or set(document) != {"schema_version", "packages"}
            or document["schema_version"] != 1 or not isinstance(document["packages"], list)
            or not document["packages"]):
        raise ImporterError("mirror list is invalid")
    seen = set()
    for entry in document["packages"]:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise ImporterError("mirror entry is invalid")
        if (not _NAME.fullmatch(str(entry["name"])) or not _FIELD.fullmatch(str(entry["version"]))
                or not _FIELD.fullmatch(str(entry["release"]))
                or entry["arch"] not in ("noarch", *ARCHITECTURES)
                or not rpm_import.SHA256.fullmatch(str(entry["sha256"]))
                or not isinstance(entry["license"], str) or not entry["license"]):
            raise ImporterError("mirror entry is invalid")
        url = urlsplit(str(entry["url"]))
        if (url.scheme != "https" or url.hostname not in START_HOSTS or url.username or url.password
                or url.port not in (None, 443) or url.query or url.fragment):
            raise ImporterError("mirror entry URL is outside the host allowlist")
        key = filename(entry)
        if key in seen:
            raise ImporterError("mirror list repeats a package")
        seen.add(key)
    return document["packages"]


def filename(entry):
    return f"{entry['name']}-{entry['version']}-{entry['release']}.{entry['arch']}.rpm"


def target_architectures(entry):
    return ARCHITECTURES if entry["arch"] == "noarch" else (entry["arch"],)


def download(url, *, limit=rpm_import.MAX_RPM_BYTES, opener=None):
    client = opener or build_opener(_NoFollow())
    for hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        allowed = START_HOSTS if hop == 0 else REDIRECT_HOSTS
        if (parts.scheme != "https" or parts.hostname not in allowed or parts.username or parts.password
                or parts.port not in (None, 443)):
            raise ImporterError("package URL is outside the host allowlist")
        try:
            response = client.open(Request(url, method="GET", headers={"User-Agent": rpm_import.USER_AGENT}),
                                   timeout=rpm_import.DOWNLOAD_TIMEOUT)
        except HTTPError as error:
            location = error.headers.get("Location", "")
            code = error.code
            error.close()
            if code in (301, 302, 303, 307, 308) and location:
                url = location
                continue
            raise ImporterError("package download failed") from None
        except (URLError, OSError, TimeoutError) as error:
            raise ImporterError("package download failed") from error
        try:
            if getattr(response, "status", 200) != 200:
                raise ImporterError("package download failed")
            return rpm_import._read_limited(response, limit)
        finally:
            response.close()
    raise ImporterError("package download redirected too many times")


class _NoFollow(rpm_import.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def inspect(path, entry):
    query = ["rpm", "-qp", "--nosignature", "--nodigest", "--queryformat"]
    fields = rpm_import._run([*query, "%{NAME}\\n%{EPOCHNUM}\\n%{VERSION}\\n%{RELEASE}\\n%{ARCH}\\n",
                              str(path)]).decode().split("\n")
    if tuple(fields[:5]) != (entry["name"], "0", entry["version"], entry["release"], entry["arch"]):
        raise ImporterError("package header does not match its pin")


def fetch_packages(work_dir, pins, *, fetcher=None):
    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    packages = {}
    for entry in pins:
        data = (fetcher or download)(entry["url"])
        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest, entry["sha256"]):
            raise ImporterError("package does not match its pinned SHA-256")
        path = directory / filename(entry)
        path.write_bytes(data)
        inspect(path, entry)
        packages[filename(entry)] = {"entry": entry, "data": data, "sha256": digest}
    return packages


def _compare_key(value):
    return [(0, int(part)) if part.isdigit() else (1, part) for part in re.findall(r"[0-9]+|[A-Za-z]+", value)]


def pin_digest(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def merge_mirror(mirrored, ledger, packages):
    """Returns (files, ledger, new) where new maps an architecture to the added file names."""
    files = {arch: dict(entries) for arch, entries in mirrored.items()}
    updated = {arch: dict(entries) for arch, entries in ledger.items()}
    new = {}
    for name, package in sorted(packages.items()):
        entry = package["entry"]
        version = f"{entry['version']}-{entry['release']}"
        for arch in target_architectures(entry):
            updated.setdefault(arch, {})
            files.setdefault(arch, {})
            known = updated[arch].get(name)
            if known is not None:
                if known["release_sha256"] != package["sha256"] or known["manifest_sha256"] != pin_digest(entry):
                    raise ImporterError("this version was already published with different content")
                continue
            for other, record in updated[arch].items():
                if (rpm_import.package_name(other) == entry["name"]
                        and _compare_key(record["version"]) >= _compare_key(version)):
                    raise ImporterError("a newer or equal version is already published")
            updated[arch][name] = {"version": version, "release_sha256": package["sha256"],
                                   "sha256": package["sha256"], "source_run_id": SOURCE_RUN,
                                   "manifest_sha256": pin_digest(entry)}
            files[arch][name] = package["data"]
            new.setdefault(arch, []).append(name)
    return files, updated, new


def prepare(work_dir, *, root=None, public=None, fetcher=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    pins = load_pins(root)
    packages = fetch_packages(Path(work_dir) / "release", pins, fetcher=fetcher)
    return packages


def publish(channel, work_dir, credentials_path, gpg_home, fingerprint, *, root=None, public=None,
            storage=None, public_verify=None, fetcher=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    work = Path(work_dir)
    packages = prepare(work, root=root, fetcher=fetcher)
    mirrored, ledger = rpm_import.mirror_channel(channel, work / "verify", public=public, trust_root=root)
    files, updated, new = merge_mirror(mirrored, ledger, packages)
    if not new:
        return {"status": "unchanged"}
    rpm_import.verify_tmpfs(gpg_home)
    fingerprint = rpm_import._check_fingerprint(fingerprint)
    identity, key_file = rpm_import._identity(channel, root)
    if fingerprint != identity["signing_subkey_fingerprint"]:
        raise ImporterError("signing key is not the configured subkey of this channel")
    stage = work / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    for arch in ARCHITECTURES:
        if not files.get(arch):
            continue
        directory = stage / FEDORA / arch / "Packages"
        directory.mkdir(parents=True)
        for name, data in files[arch].items():
            (directory / name).write_bytes(data)
        if arch in new:
            rpm_import.sign_packages(directory, new[arch], gpg_home, fingerprint)
            for name in new[arch]:
                updated[arch][name]["sha256"] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
    rpm_import.build_repository(stage, gpg_home, fingerprint, updated)
    credentials = r2_probe.load_credentials(credentials_path)
    if credentials.get("bucket") != issuer.BUCKET or credentials.get("prefix") != channel + "/":
        raise ImporterError("temporary credential scope does not match channel")
    storage = storage or rpm_import.SigV4Storage(
        issuer.BUCKET, "https://89e6fc75aed6d04cd84a0c7d11a5594e.r2.cloudflarestorage.com", credentials)
    extra = {rpm_import.KEY_NAME: key_file.read_bytes()}
    repo_file = root / "descriptors" / channel / f"luminus-{channel}.repo"
    if repo_file.is_file():
        extra[f"luminus-{channel}.repo"] = repo_file.read_bytes()
    overwrite = {f"{FEDORA}/{arch}/Packages/{name}" for arch, names in new.items() for name in names}
    plan = rpm_import.plan_uploads(stage, channel, storage.list_keys(f"{channel}/"), extra, overwrite)
    written = rpm_import.upload(plan, channel, credentials, storage=storage, public_verify=public_verify)
    return {"status": "imported", "objects": written,
            "packages": {arch: list(names) for arch, names in new.items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publish the pinned mirror packages into a channel")
    parser.add_argument("--mode", choices=("prepare", "publish"), required=True)
    parser.add_argument("--channel", choices=rpm_import.CHANNELS, required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--gpg-home")
    parser.add_argument("--signing-fingerprint")
    parser.add_argument("--credentials")
    args = parser.parse_args(argv)
    try:
        if args.mode == "prepare":
            root = Path(__file__).resolve().parents[1]
            packages = prepare(args.work_dir, root=root)
            mirrored, ledger = rpm_import.mirror_channel(args.channel, Path(args.work_dir) / "verify",
                                                         trust_root=root)
            _files, _updated, new = merge_mirror(mirrored, ledger, packages)
            result = {"status": "ready" if new else "unchanged",
                      "new": {arch: list(names) for arch, names in new.items()}}
        else:
            result = publish(args.channel, args.work_dir, args.credentials, args.gpg_home,
                             args.signing_fingerprint)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ImporterError as error:
        print("RPM mirror failed:", error, file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError) as error:
        print("RPM mirror failed:", type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
