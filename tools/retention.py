#!/usr/bin/env python3
"""Plans which packages of a channel could be removed. It only reports; it never deletes.

A channel keeps its newest versions and every pinned file. Pins name files that an
image build or a recovery procedure still depends on, for example the Sirius package
inside a published installer image.
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import distribution_contract  # noqa: E402

KEEP_NEWEST = 3


def plan(ledger, pins=(), keep=KEEP_NEWEST):
    result = {}
    for arch, entries in sorted(ledger.items()):
        ordered = sorted(entries, key=lambda name: distribution_contract._version_key(entries[name]["version"]),
                         reverse=True)
        kept = set(ordered[:keep]) | (set(pins) & set(entries))
        result[arch] = {"keep": [name for name in ordered if name in kept],
                        "removable": [name for name in ordered if name not in kept]}
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
