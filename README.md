# Luminus RPM repository

Catalog and validation tools for Luminus RPM distribution. `catalog.json`
registers `sirius-installer` for Fedora 45, x86_64/aarch64 and stable/testing.
Signed RPMs are published to `https://rpm.luminusos.org` by the publish workflow, see
`docs/PUBLISHING.md`. To use the repository on Fedora, see `docs/CLIENTS.md`.

## Run the checks

Requires Python 3.10 or newer. The repository tests that build, sign and install real
packages need `rpm-build`, `rpm-sign`, `createrepo_c`, `gnupg2` and `dnf5`, and are skipped
without them; the CI job runs in a Fedora 45 container so they always run there.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements-dev.txt
.venv/bin/python -B -m unittest discover -s contracts -p 'test_*.py' -v
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python tools/catalog.py catalog.json
```

## Extend the catalog

Add a package ID and its source repository/workflow to `catalog.json`, then run
the checks above. Fedora 45, both architectures and both channels remain
required. Validation does not fetch builds, approve releases, or publish RPMs; only the
publish workflow does.

`contracts/` is the frozen artifact-contract v1 snapshot, with SHA-256 values
in `contracts.lock.json`. It still accepts release artifacts only for the three
initially approved applications. A new catalog entry does not extend that
release allowlist; revise the contract and its consumers before publishing a
new package. Update matching snapshots in both infrastructure repositories
together, with review and tests. Do not edit individual snapshot files locally.

## Workflows

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| Validate catalogs and contracts | pull request, push to `main` | Runs the tests and checks the catalog. Its `validate` job is the required check. |
| Check R2 credential scope | manual, protected `main` | Issues short-lived channel credentials and can probe what they may and may not do. See `docs/ISSUER.md`. |
| Check environment protections | pull request, manual | Shows that pull requests cannot enter a signing environment and that dispatches are authorized. |
| Publish RPM release | manual, protected `main` | Verifies a Sirius release, signs its packages and uploads one channel. See `docs/PUBLISHING.md`. |
