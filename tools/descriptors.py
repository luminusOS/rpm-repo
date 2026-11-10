#!/usr/bin/env python3
"""Renders the DNF .repo files from the catalog. `write` updates them, `check` fails on drift."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://rpm.luminusos.org"
REMOTES = {"stable": ("luminus", "Luminus OS (stable)"), "testing": ("luminus-testing", "Luminus OS (testing)")}


def render(channel, catalog):
    if channel not in REMOTES or catalog.get("fedora") != 45 or catalog.get("format") != "rpm":
        raise ValueError("catalog does not describe this repository")
    section, title = REMOTES[channel]
    return (
        f"[{section}]\n"
        f"name={title}\n"
        f"baseurl={BASE}/{channel}/{catalog['fedora']}/$basearch/\n"
        "enabled=1\n"
        "gpgcheck=1\n"
        "repo_gpgcheck=1\n"
        f"gpgkey={BASE}/{channel}/luminus.asc\n"
        "metadata_expire=1h\n"
    )


def expected(root=ROOT):
    catalog = json.loads((Path(root) / "catalog.json").read_text())
    return {Path("descriptors") / channel / f"luminus-{channel}.repo": render(channel, catalog)
            for channel in catalog["channels"]}


def main(argv=None):
    command = (argv or sys.argv[1:] or [""])[0]
    wanted = expected()
    if command == "write":
        for relative, text in wanted.items():
            (ROOT / relative).parent.mkdir(parents=True, exist_ok=True)
            (ROOT / relative).write_text(text)
        return 0
    if command == "check":
        stale = [str(relative) for relative, text in wanted.items()
                 if not (ROOT / relative).is_file() or (ROOT / relative).read_text() != text]
        if stale:
            print("descriptors are out of date:", ", ".join(stale), file=sys.stderr)
            return 1
        print(json.dumps({"status": "ok", "files": len(wanted)}))
        return 0
    print("usage: descriptors.py write|check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
