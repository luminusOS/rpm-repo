#!/usr/bin/env python3
"""Imports a verified Sirius RPM release into one channel of the DNF repository.

The release is identified by the dispatch inputs. Its packages are downloaded from
the source GitHub release, checked against the published checksums and against the
RPM headers, and merged with the packages already served by the channel. The
channel's own signature is verified on everything that is mirrored before anything
new is added, so a damaged or tampered public repository stops the import.
"""

import argparse
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import distribution_contract
import issuer
import r2_probe
import trust


CHANNELS = ("stable", "testing")
ARCHITECTURES = ("x86_64", "aarch64")
FEDORA = "45"
PACKAGE = "sirius-installer"
PUBLIC_BASE = "https://rpm.luminusos.org"
CHECKSUMS_NAME = "checksums.txt"
ARTIFACT_NAME = "sirius-rpm-{arch}"
LEDGER = "luminus-imports.json"
KEY_NAME = "luminus.asc"
MAX_CHECKSUMS_BYTES = 64 * 1024
MAX_RPM_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30
PUBLIC_VERIFY_TIMEOUT = 120
USER_AGENT = "luminus-rpm-publisher/1"
REDIRECT_ASSET_HOSTS = {"objects.githubusercontent.com", "release-assets.githubusercontent.com"}
ALLOWED_ASSET_HOSTS = {"github.com"} | REDIRECT_ASSET_HOSTS
TAG_PATTERN = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-rc\.?[1-9][0-9]*)?\Z")
RPM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}\.rpm\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
MUTABLE_PHASE_BUDGET_SECONDS = 120
MAX_LIST_PAGES = 2000


class ImporterError(Exception):
    pass


@dataclass(frozen=True)
class FetchPlan:
    repository: str
    tag: str
    checksums_url: str
    asset_urls: dict


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def rpm_version(version):
    return version.replace("-rc.", "~rc.")


_FILENAME = re.compile(r"(.+?)-v?[0-9][^/]*\.[^./]+\.rpm\Z")


def package_name(filename):
    match = _FILENAME.fullmatch(filename)
    if not match:
        raise ImporterError("package file name is invalid")
    return match.group(1)


def rpm_filename(version, arch):
    return f"{PACKAGE}-{version}-1.{arch}.rpm"


def validate_descriptor(descriptor):
    if not isinstance(descriptor, dict) or set(descriptor) != {
        "format", "channel", "package_id", "version", "tag", "git_sha", "source_run_id",
        "source_run_attempt",
    }:
        raise ImporterError("release descriptor is invalid")
    try:
        basic = {key: value for key, value in descriptor.items() if key != "tag"}
        issuer.validate_descriptor(basic, issuer.FORMAT, descriptor["channel"])
    except (issuer.IssuerError, KeyError, TypeError) as error:
        raise ImporterError("release descriptor is invalid") from error
    tag = descriptor["tag"]
    if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
        raise ImporterError("release tag is invalid")
    if re.sub(r"-rc\.?([1-9][0-9]*)$", r"-rc.\1", tag[1:]) != descriptor["version"]:
        raise ImporterError("release tag and version disagree")
    if descriptor["package_id"] != PACKAGE:
        raise ImporterError("release descriptor is invalid")
    if "-rc." in descriptor["version"] and descriptor["channel"] != "testing":
        raise ImporterError("pre-release versions are testing-only")
    return {**descriptor, "git_sha": descriptor["git_sha"].lower()}


def fetch_plan(descriptor):
    checked = validate_descriptor(descriptor)
    repository = issuer.SOURCE_RELEASES[PACKAGE][0]
    base = f"https://github.com/{repository}/releases/download/{checked['tag']}"
    return FetchPlan(repository, checked["tag"], f"{base}/{CHECKSUMS_NAME}",
                     {arch: f"{base}/{rpm_filename(checked['version'], arch)}" for arch in ARCHITECTURES})


_CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]{0,199})\Z")


def parse_checksums(content):
    try:
        lines = content.decode("utf-8", "strict").split("\n")
    except UnicodeError as error:
        raise ImporterError("release checksums are invalid") from error
    if lines and lines[-1] == "":
        lines.pop()
    result = {}
    for line in lines:
        match = _CHECKSUM_LINE.fullmatch(line)
        if not match or match.group(2) in result:
            raise ImporterError("release checksums are invalid")
        result[match.group(2)] = match.group(1)
    if not result:
        raise ImporterError("release checksums are invalid")
    return result


def list_rpm_artifacts(repository, run_id, fetcher=None):
    listing = (fetcher or issuer.fetch_github_json)(f"/repos/{repository}/actions/runs/{run_id}/artifacts")
    entries = listing.get("artifacts") if isinstance(listing, dict) else None
    if not isinstance(entries, list):
        raise ImporterError("source run artifact listing is invalid")
    found = {}
    for arch in ARCHITECTURES:
        name = ARTIFACT_NAME.format(arch=arch)
        matches = [item for item in entries if isinstance(item, dict) and item.get("name") == name
                   and item.get("expired") is False]
        if len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] < 1:
            raise ImporterError("source run does not list exactly one package artifact per architecture")
        found[arch] = matches[0]["id"]
    if len(set(found.values())) != len(found):
        raise ImporterError("source run artifact identifiers are not distinct")
    return found


def build_manifest(descriptor, packages, artifact_ids):
    checked = validate_descriptor(descriptor)
    repository, _caller, workflow, _format = issuer.SOURCE_RELEASES[PACKAGE]
    return {
        "schema_version": 1, "kind": "release", "format": "rpm", "application": PACKAGE,
        "version": checked["version"],
        "source": {"repository": repository, "workflow": workflow, "tag": checked["tag"],
                   "revision": checked["git_sha"], "run_id": int(checked["source_run_id"]),
                   "run_attempt": checked["source_run_attempt"]},
        "artifacts": {arch: {"artifact_id": artifact_ids[arch], "filename": packages[arch]["filename"],
                             "sha256": packages[arch]["sha256"], "size_bytes": len(packages[arch]["data"])}
                      for arch in ARCHITECTURES},
    }


def validate_manifest(manifest, descriptor):
    checked = validate_descriptor(descriptor)
    try:
        distribution_contract.validate_release(manifest)
    except (ValueError, KeyError, TypeError) as error:
        raise ImporterError("release manifest is invalid") from error
    source = manifest["source"]
    repository, _caller, workflow, _format = issuer.SOURCE_RELEASES[PACKAGE]
    if (manifest["format"] != "rpm" or manifest["application"] != PACKAGE
            or manifest["version"] != checked["version"]
            or source["repository"] != repository or source["workflow"] != workflow
            or source["tag"] != checked["tag"] or source["revision"] != checked["git_sha"]
            or str(source["run_id"]) != checked["source_run_id"]
            or source["run_attempt"] != checked["source_run_attempt"]):
        raise ImporterError("manifest provenance does not match the release descriptor")
    for arch in ARCHITECTURES:
        artifact = manifest["artifacts"][arch]
        if artifact["filename"] != rpm_filename(checked["version"], arch) or artifact["size_bytes"] > MAX_RPM_BYTES:
            raise ImporterError("manifest artifact name or size is invalid")
    return manifest


def _read_limited(response, limit):
    length = response.headers.get("Content-Length")
    if length is not None and (not length.isdigit() or int(length) > limit):
        raise ImporterError("release asset exceeds size limit")
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ImporterError("release asset exceeds size limit")
    return data


def download_asset(url, *, limit, opener=None):
    original = urlsplit(url)
    if original.scheme != "https" or original.hostname != "github.com" or original.username or original.password:
        raise ImporterError("release asset URL is outside the fixed host allowlist")
    client = opener or build_opener(_NoRedirect())
    try:
        response = client.open(Request(url, method="GET"), timeout=DOWNLOAD_TIMEOUT)
    except HTTPError as error:
        if error.code not in (301, 302, 303, 307, 308):
            error.close()
            raise ImporterError("release asset download failed") from None
        location = error.headers.get("Location", "")
        error.close()
        target = urlsplit(location)
        if (target.scheme != "https" or target.hostname not in REDIRECT_ASSET_HOSTS or target.username
                or target.password or target.port not in (None, 443)):
            raise ImporterError("release asset redirect is not allowed") from None
        try:
            response = client.open(Request(location, method="GET"), timeout=DOWNLOAD_TIMEOUT)
        except (HTTPError, URLError, OSError, TimeoutError) as nested:
            raise ImporterError("release asset download failed") from nested
    except (URLError, OSError, TimeoutError) as error:
        raise ImporterError("release asset download failed") from error
    try:
        final_url = urlsplit(response.geturl())
        if (getattr(response, "status", 200) != 200 or final_url.scheme != "https"
                or final_url.hostname not in ALLOWED_ASSET_HOSTS or final_url.username
                or final_url.password or final_url.port not in (None, 443)):
            raise ImporterError("release asset download failed")
        return _read_limited(response, limit)
    finally:
        response.close()


def _run(command, *, timeout=120, input_data=None, env=None):
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, input=input_data, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ImporterError(f"{Path(str(command[0])).name} could not run") from error
    if result.returncode:
        raise ImporterError(f"{Path(str(command[0])).name} failed")
    return result.stdout


def inspect_rpm(path, descriptor, arch):
    checked = validate_descriptor(descriptor)
    fields = _run(["rpm", "-qp", "--nosignature", "--nodigest", "--queryformat",
                   "%{NAME}\\n%{EPOCHNUM}\\n%{VERSION}\\n%{RELEASE}\\n%{ARCH}\\n", str(path)]).decode().split("\n")
    name, epoch, version, release, architecture = fields[:5]
    if (name, epoch, version, release, architecture) != (
            PACKAGE, "0", rpm_version(checked["version"]), "1", arch):
        raise ImporterError("package header does not match the release")
    files = _run(["rpm", "-qp", "--nosignature", "--nodigest", "--queryformat", "[%{FILENAMES}\\n]",
                  str(path)]).decode().split("\n")
    if "/usr/bin/sirius" not in files or "/etc/sirius/distro.toml" in files or any(
            "/polkit-1/rules.d/" in item for item in files):
        raise ImporterError("package contents are not the generic Sirius package")
    return {"name": name, "epoch": 0, "version": version, "release": release, "arch": arch,
            "filename": Path(path).name}


def fetch_release(descriptor, work_dir, *, artifact_lister=None):
    plan = fetch_plan(descriptor)
    checksums = parse_checksums(download_asset(plan.checksums_url, limit=MAX_CHECKSUMS_BYTES))
    packages = {}
    for arch, url in plan.asset_urls.items():
        filename = urlsplit(url).path.rsplit("/", 1)[1]
        data = download_asset(url, limit=MAX_RPM_BYTES)
        digest = hashlib.sha256(data).hexdigest()
        if filename not in checksums or not hmac.compare_digest(digest, checksums[filename]):
            raise ImporterError("release asset does not match the published checksums")
        packages[arch] = {"filename": filename, "sha256": digest, "data": data}
    artifact_ids = (artifact_lister or list_rpm_artifacts)(plan.repository, descriptor["source_run_id"])
    manifest = validate_manifest(build_manifest(descriptor, packages, artifact_ids), descriptor)
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    checked = {**descriptor, "manifest_sha256": hashlib.sha256(canonical).hexdigest()}
    paths = {}
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    for arch in ARCHITECTURES:
        paths[arch] = Path(work_dir) / manifest["artifacts"][arch]["filename"]
        paths[arch].write_bytes(packages[arch]["data"])
    inspected = {arch: inspect_rpm(paths[arch], descriptor, arch) for arch in ARCHITECTURES}
    for arch in ARCHITECTURES:
        inspected[arch]["sha256"] = packages[arch]["sha256"]
    return checked, paths, inspected


class _Public:
    def __init__(self, getter=None):
        self.getter = getter or self.fetch

    @staticmethod
    def fetch(url, limit):
        try:
            with build_opener(_NoRedirect()).open(Request(url, method="GET", headers={"User-Agent": USER_AGENT}),
                                                  timeout=PUBLIC_VERIFY_TIMEOUT) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ImporterError("public repository response is invalid")
                return _read_limited(response, limit)
        except HTTPError as error:
            error.close()
            if error.code == 404:
                return None
            raise ImporterError("public repository state cannot be read") from None
        except (OSError, TimeoutError, URLError) as error:
            raise ImporterError("public repository state cannot be read") from error

    def get(self, channel, relative, limit=MAX_METADATA_BYTES):
        return self.getter(f"{PUBLIC_BASE}/{channel}/{relative}", limit)


def _identity(channel, root):
    try:
        manifest = trust.load_manifest(root / "trust.json")
        if manifest["status"] != "provisioned":
            raise ValueError("channel trust is not provisioned")
        identity = manifest["channels"][channel]
        return identity, trust.resolve_key_path(root, identity["public_key"])
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as error:
        raise ImporterError("channel trust cannot be loaded") from error


def _verify_detached(root, channel, content, signature, work_dir):
    identity, public_key = _identity(channel, root)
    signature_path = Path(work_dir) / "verify.sig"
    content_path = Path(work_dir) / "verify.data"
    signature_path.write_bytes(signature)
    content_path.write_bytes(content)
    try:
        valid = trust.verify_signature(public_key, signature_path, content_path, identity["primary_fingerprint"],
                                       identity["signing_subkey_fingerprint"])
    except (OSError, ValueError, RuntimeError) as error:
        raise ImporterError("channel repository authentication failed") from error
    finally:
        signature_path.unlink(missing_ok=True)
        content_path.unlink(missing_ok=True)
    if not valid:
        raise ImporterError("channel repository authentication failed")


def _is_legacy_ledger(content):
    try:
        document = json.loads(content.decode("utf-8")) if content is not None else None
    except (UnicodeError, ValueError):
        return False
    return isinstance(document, dict) and "packages" in document and "ledger" not in document


def open_ledger(root, channel, envelope, work_dir, legacy_signature=None):
    """Verify the signed ledger envelope and return the ledger text, or None if absent.

    A channel written before the envelope existed has the ledger and a detached
    `.sig` object; it is still verified and is replaced by an envelope on the next
    publication.

    The ledger and its signature travel in one object so that a single write replaces
    both, and an interrupted publication can never leave a ledger without its signature.
    """
    if envelope is None:
        return None
    if _is_legacy_ledger(envelope):
        if legacy_signature is None:
            raise ImporterError("import ledger signature is missing")
        _verify_detached(root, channel, envelope, legacy_signature, work_dir)
        return envelope
    try:
        document = json.loads(envelope.decode("utf-8"))
        content = document["ledger"].encode("utf-8")
        signature = document["signature"].encode("utf-8")
        if set(document) != {"ledger", "signature"}:
            raise ValueError("unexpected fields")
    except (UnicodeError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise ImporterError("import ledger is invalid") from error
    _verify_detached(root, channel, content, signature, work_dir)
    return content


def load_ledger(content):
    if content is None:
        return {}
    try:
        ledger = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ImporterError("import ledger is invalid") from error
    if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "packages"}
            or ledger["schema_version"] != 1 or not isinstance(ledger["packages"], dict)):
        raise ImporterError("import ledger is invalid")
    for arch, entries in ledger["packages"].items():
        if arch not in ARCHITECTURES or not isinstance(entries, dict):
            raise ImporterError("import ledger is invalid")
        for filename, entry in entries.items():
            if (not RPM_NAME.fullmatch(filename) or not isinstance(entry, dict)
                    or set(entry) != {"version", "release_sha256", "sha256", "source_run_id", "manifest_sha256"}
                    or not isinstance(entry["version"], str) or not SHA256.fullmatch(entry["sha256"])
                    or not SHA256.fullmatch(entry["release_sha256"])
                    or not SHA256.fullmatch(entry["manifest_sha256"])):
                raise ImporterError("import ledger is invalid")
    return ledger["packages"]


def mirror_channel(channel, work_dir, *, public=None, trust_root=None):
    """Return ({arch: {filename: bytes}}, ledger) for what the channel serves today.

    The signed ledger and the package hashes it records are the only inputs that are
    trusted. Repository metadata is derived data: it is rebuilt from the packages on
    every publication, so metadata left half-written by an interrupted run does not
    block the next one and cannot influence what is mirrored.
    """
    root = Path(trust_root or Path(__file__).resolve().parents[1])
    public = public or _Public()
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    envelope = public.get(channel, LEDGER, 1024 * 1024)
    legacy_signature = public.get(channel, LEDGER + ".sig", 64 * 1024) if _is_legacy_ledger(envelope) else None
    ledger_bytes = open_ledger(root, channel, envelope, work, legacy_signature)
    ledger = load_ledger(ledger_bytes)
    mirrored = {}
    for arch in ARCHITECTURES:
        files = {}
        for filename, entry in ledger.get(arch, {}).items():
            data = public.get(channel, f"{FEDORA}/{arch}/Packages/{filename}", MAX_RPM_BYTES)
            if data is None or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), entry["sha256"]):
                raise ImporterError("a published package differs from the signed ledger")
            files[filename] = data
        mirrored[arch] = files
    return mirrored, ledger


def _version_key(version):
    return distribution_contract._version_key(version)


def merge_release(mirrored, ledger, checked, paths, inspected):
    """Add the new packages to the mirrored set. Returns (files, ledger, new_files).

    The ledger digest of a new package is that of the unsigned release asset until
    the package is signed; the publisher replaces it with the digest of the signed file."""
    version = checked["version"]
    files = {arch: dict(entries) for arch, entries in mirrored.items()}
    updated = {arch: dict(entries) for arch, entries in ledger.items()}
    for arch in ARCHITECTURES:
        updated.setdefault(arch, {})
        name = inspected[arch]["filename"]
        digest = inspected[arch]["sha256"]
        known = updated[arch].get(name)
        if known is not None:
            if known["release_sha256"] != digest or known["manifest_sha256"] != checked["manifest_sha256"]:
                raise ImporterError("this version was already published with different content")
            continue
        for other, entry in updated[arch].items():
            if package_name(other) == PACKAGE and _version_key(entry["version"]) >= _version_key(version):
                raise ImporterError("a newer or equal version is already published")
        updated[arch][name] = {"version": version, "release_sha256": digest, "sha256": digest,
                               "source_run_id": checked["source_run_id"],
                               "manifest_sha256": checked["manifest_sha256"]}
        files[arch][name] = Path(paths[arch]).read_bytes()
    new = {arch: name for arch in ARCHITECTURES if (name := inspected[arch]["filename"]) not in ledger.get(arch, {})}
    return files, updated, new


def verify_tmpfs(path):
    home = Path(path)
    try:
        filesystem = subprocess.run(["stat", "-f", "-c", "%T", str(home)], check=True,
                                    capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ImporterError("GnuPG home filesystem could not be verified") from error
    if filesystem != "tmpfs":
        raise ImporterError("GnuPG home must reside on tmpfs")
    if home.exists() and home.stat().st_mode & 0o077:
        raise ImporterError("GnuPG home permissions are too broad")
    return home


def _check_fingerprint(value):
    if not re.fullmatch(r"[A-F0-9]{40,64}", value or ""):
        raise ImporterError("channel signing fingerprint is invalid")
    return value


def sign_packages(directory, names, gpg_home, fingerprint):
    for name in names:
        _run(["rpmsign", "--define", f"_gpg_path {gpg_home}", "--define", f"_gpg_name {fingerprint}!",
              "--addsign", str(Path(directory) / name)], timeout=300)


def build_repository(stage, gpg_home, fingerprint, ledger):
    """Write signed metadata for every architecture directory under stage."""
    for arch in ARCHITECTURES:
        directory = Path(stage) / FEDORA / arch
        if not (directory / "Packages").is_dir():
            continue
        shutil.rmtree(directory / "repodata", ignore_errors=True)
        _run(["createrepo_c", "--general-compress-type=gz", "--checksum=sha256", "--no-database",
              str(directory)], timeout=600)
        _run(["gpg", "--batch", "--yes", "--homedir", str(gpg_home), "--no-options", "--local-user",
              fingerprint + "!", "--armor", "--detach-sign", "--output", str(directory / "repodata/repomd.xml.asc"),
              str(directory / "repodata/repomd.xml")], timeout=120)
    ledger_path = Path(stage) / LEDGER
    content = json.dumps({"schema_version": 1, "packages": ledger}, sort_keys=True, indent=2) + "\n"
    unsigned = Path(stage).parent / "ledger.json"
    unsigned.write_text(content, encoding="utf-8")
    signature_path = Path(stage).parent / "ledger.json.asc"
    signature_path.unlink(missing_ok=True)
    _run(["gpg", "--batch", "--yes", "--homedir", str(gpg_home), "--no-options", "--local-user",
          fingerprint + "!", "--armor", "--detach-sign", "--output", str(signature_path), str(unsigned)], timeout=120)
    ledger_path.write_text(json.dumps({"ledger": content, "signature": signature_path.read_text(encoding="ascii")},
                                      sort_keys=True), encoding="utf-8")


def _rank(relative):
    if relative.endswith(".rpm"):
        return 0
    if relative.endswith("repodata/repomd.xml"):
        return 2
    if relative.endswith("repodata/repomd.xml.asc"):
        return 3
    if "/repodata/" in relative:
        return 1
    if relative == LEDGER:
        return 4
    return 6


def _checked_key(channel, key):
    if channel not in CHANNELS or not isinstance(key, str) or not key.startswith(channel + "/"):
        raise ImporterError("object key is outside the selected channel")
    pieces = key.split("/")
    if "\\" in key or "\x00" in key or any(piece in ("", ".", "..") for piece in pieces):
        raise ImporterError("object key contains an unsafe path")
    return key


def plan_uploads(stage, channel, remote_sizes, extra_files=None, overwrite=()):
    files = {}
    for path in sorted(Path(stage).rglob("*")):
        if path.is_file():
            files[path.relative_to(stage).as_posix()] = path.read_bytes()
    files.update(extra_files or {})
    plan = []
    for relative, content in files.items():
        key = f"{channel}/{relative}"
        _checked_key(channel, key)
        if relative.endswith(".rpm") and key in remote_sizes and relative not in overwrite:
            if remote_sizes[key] != len(content):
                raise ImporterError("a published package differs from the staged package")
            continue
        plan.append((_rank(relative), key, content))
    return sorted(plan, key=lambda item: (item[0], item[1]))


class SigV4Storage:
    def __init__(self, bucket, endpoint, credentials):
        self.bucket = bucket
        self.client = r2_probe.SigV4Client(credentials, host=urlsplit(endpoint).hostname)

    def put(self, key, content):
        response = self.client._request("PUT", self.bucket, key, body=content)
        if response.status not in (200, 201, 204):
            raise ImporterError("R2 object upload failed")

    def list_keys(self, prefix):
        sizes = {}
        token = None
        for _ in range(MAX_LIST_PAGES):
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            response = self.client._request("GET", self.bucket, query=query)
            if response.status != 200:
                raise ImporterError("R2 listing failed")
            try:
                root = ElementTree.fromstring(response.body)
            except ElementTree.ParseError as error:
                raise ImporterError("R2 listing is invalid") from error
            truncated, token = False, None
            for element in root:
                name = element.tag.rsplit("}", 1)[-1]
                if name == "Contents":
                    fields = {child.tag.rsplit("}", 1)[-1]: child.text for child in element}
                    if not fields.get("Key", "").startswith(prefix) or not (fields.get("Size") or "").isdigit():
                        raise ImporterError("R2 listing is invalid")
                    sizes[fields["Key"]] = int(fields["Size"])
                elif name == "IsTruncated":
                    truncated = (element.text or "").strip().lower() == "true"
                elif name == "NextContinuationToken":
                    token = element.text
            if not truncated:
                return sizes
            if not token:
                raise ImporterError("R2 listing is invalid")
        raise ImporterError("R2 listing is too large")


def verify_public_object(key, content):
    url = f"{PUBLIC_BASE}/{key}"
    for attempt in range(3):
        try:
            observed = _Public.fetch(url, len(content) + 1)
            return observed is not None and len(observed) == len(content) and hmac.compare_digest(
                hashlib.sha256(observed).digest(), hashlib.sha256(content).digest())
        except ImporterError:
            if attempt == 2:
                return False
            time.sleep(attempt + 1)
    return False


def upload(plan, channel, credentials, *, storage=None, public_verify=None, clock=time.time,
           mutable_budget=MUTABLE_PHASE_BUDGET_SECONDS):
    """Upload immutable packages first, metadata last. Returns the number of objects written."""
    verify = public_verify or verify_public_object
    expires_at = credentials["expires_at"]
    packages = [item for item in plan if item[0] == 0]
    metadata = [item for item in plan if item[0] != 0]
    for _rank_value, key, content in packages:
        if clock() >= expires_at:
            raise ImporterError("temporary credentials expired during package uploads")
        storage.put(key, content)
        if not verify(key, content):
            raise ImporterError("public package verification failed")
    if metadata and expires_at - clock() < mutable_budget:
        raise ImporterError("insufficient temporary credential lifetime for repository metadata")
    for _rank_value, key, content in metadata:
        if clock() >= expires_at:
            raise ImporterError("temporary credentials expired during metadata uploads")
        storage.put(key, content)
    for _rank_value, key, content in metadata:
        if not verify(key, content):
            raise ImporterError("public metadata verification failed")
    return len(plan)


def publish_release(descriptor, work_dir, credentials_path, gpg_home, fingerprint, *, root=None,
                    public=None, storage=None, public_verify=None, artifact_lister=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    work = Path(work_dir)
    checked, paths, inspected = fetch_release(descriptor, work / "release", artifact_lister=artifact_lister)
    channel = checked["channel"]
    mirrored, ledger = mirror_channel(channel, work / "verify", public=public, trust_root=root)
    files, updated, new = merge_release(mirrored, ledger, checked, paths, inspected)
    if not new:
        return {"status": "unchanged", "version": checked["version"]}
    verify_tmpfs(gpg_home)
    fingerprint = _check_fingerprint(fingerprint)
    if fingerprint != _identity(channel, root)[0]["signing_subkey_fingerprint"]:
        raise ImporterError("signing key is not the configured subkey of this channel")
    stage = work / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    for arch in ARCHITECTURES:
        packages = stage / FEDORA / arch / "Packages"
        packages.mkdir(parents=True)
        for name, data in files[arch].items():
            (packages / name).write_bytes(data)
        if arch in new:
            sign_packages(packages, [new[arch]], gpg_home, fingerprint)
            updated[arch][new[arch]]["sha256"] = hashlib.sha256((packages / new[arch]).read_bytes()).hexdigest()
    build_repository(stage, gpg_home, fingerprint, updated)
    credentials = r2_probe.load_credentials(credentials_path)
    if credentials.get("bucket") != issuer.BUCKET or credentials.get("prefix") != channel + "/":
        raise ImporterError("temporary credential scope does not match channel")
    storage = storage or SigV4Storage(issuer.BUCKET, "https://89e6fc75aed6d04cd84a0c7d11a5594e.r2.cloudflarestorage.com",
                                      credentials)
    key_file = trust.resolve_key_path(root, _identity(channel, root)[0]["public_key"])
    extra = {KEY_NAME: key_file.read_bytes()}
    repo_file = root / "descriptors" / channel / f"luminus-{channel}.repo"
    if repo_file.is_file():
        extra[f"luminus-{channel}.repo"] = repo_file.read_bytes()
    overwrite = {f"{FEDORA}/{arch}/Packages/{name}" for arch, name in new.items()}
    plan = plan_uploads(stage, channel, storage.list_keys(f"{channel}/"), extra, overwrite)
    written = upload(plan, channel, credentials, storage=storage, public_verify=public_verify)
    return {"status": "imported", "version": checked["version"], "objects": written,
            "packages": dict(new)}


def prepare_release(descriptor, work_dir, *, root=None, public=None, artifact_lister=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    work = Path(work_dir)
    checked, paths, inspected = fetch_release(descriptor, work / "release", artifact_lister=artifact_lister)
    mirrored, ledger = mirror_channel(checked["channel"], work / "verify", public=public, trust_root=root)
    _files, _updated, new = merge_release(mirrored, ledger, checked, paths, inspected)
    return {"status": "ready" if new else "unchanged", "version": checked["version"], "new": new}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import a verified Sirius RPM release into a channel")
    parser.add_argument("--mode", choices=("prepare", "publish"), required=True)
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--gpg-home")
    parser.add_argument("--signing-fingerprint")
    parser.add_argument("--credentials")
    args = parser.parse_args(argv)
    try:
        descriptor = json.loads(Path(args.descriptor).read_text(encoding="utf-8"))
        if args.mode == "prepare":
            result = prepare_release(descriptor, args.work_dir)
        else:
            result = publish_release(descriptor, args.work_dir, args.credentials, args.gpg_home,
                                     args.signing_fingerprint)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ImporterError as error:
        print("RPM import failed:", error, file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        frame = error.__traceback__
        while frame is not None and frame.tb_next is not None:
            frame = frame.tb_next
        where = f"{Path(frame.tb_frame.f_code.co_filename).name}:{frame.tb_lineno}" if frame else "unknown"
        print("RPM import failed:", type(error).__name__, "at", where, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
