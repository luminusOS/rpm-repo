# Luminus RPM repository

Catalog and validation tools for Luminus RPM distribution. `catalog.json`
registers `sirius-installer` for Fedora 45, x86_64/aarch64 and stable/testing.
Package publishing and the public DNF repository are not implemented yet.

## Run the checks

Requires Python 3.10 or newer.

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
required. Validation does not fetch builds, approve releases, or publish RPMs.

`contracts/` is the frozen T01 artifact-contract v1 snapshot, with SHA-256 values
in `contracts.lock.json`. It still accepts release artifacts only for the three
initially approved applications. A new catalog entry does not extend that
release allowlist; revise the contract and its consumers before publishing a
new package. Update matching snapshots in both infrastructure repositories
together, with review and tests. Do not edit individual snapshot files locally.
