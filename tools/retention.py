#!/usr/bin/env python3
"""Plans which packages of a channel could be removed. It only reports; it never deletes.

A channel keeps its newest versions and every pinned file. Pins name files that an
image build or a recovery procedure still depends on, for example the Sirius package
inside a published installer image.
"""

import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import distribution_contract  # noqa: E402

KEEP_NEWEST = 3


def _key(version):
    return [(0, int(part)) if part.isdigit() else (1, part) for part in re.findall(r"[0-9]+|[A-Za-z]+", version)]


def _sort_key(entry):
    try:
        return (0, distribution_contract._version_key(entry["version"]))
    except (ValueError, KeyError, TypeError):
        return (1, _key(entry["version"]))


def _name(filename):
    match = re.fullmatch(r"(.+?)-v?[0-9][^/]*\.[^./]+\.rpm", filename)
    return match.group(1) if match else ""


def plan(ledger, pins=(), keep=KEEP_NEWEST):
    result = {}
    for arch, entries in sorted(ledger.items()):
        keep_names = set(pins) & set(entries)
        ordered = []
        for package in sorted({_name(name) for name in entries}):
            versions = [name for name in entries if _name(name) == package]
            versions.sort(key=lambda name: _sort_key(entries[name]), reverse=True)
            ordered.extend(versions)
            keep_names.update(versions[:keep])
        result[arch] = {"keep": [name for name in ordered if name in keep_names],
                        "removable": [name for name in ordered if name not in keep_names]}
    return result


def main(argv=None):
    arguments = argv if argv is not None else sys.argv[1:]
    if len(arguments) not in (1, 2):
        print("usage: retention.py LEDGER_JSON [PINS_JSON]", file=sys.stderr)
        return 2
    envelope = json.loads(Path(arguments[0]).read_text())
    ledger = json.loads(envelope["ledger"])["packages"] if "ledger" in envelope else envelope["packages"]
    pins = json.loads(Path(arguments[1]).read_text()) if len(arguments) == 2 else []
    print(json.dumps(plan(ledger, pins), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
