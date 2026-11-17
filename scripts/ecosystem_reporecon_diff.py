#!/usr/bin/env python3
"""List RepoRecon repos missing from the ecosystem registry (Issue #6749).

Reports only; never edits ``projects.toml`` (registry rule 2: facts are
human-ratified). Usage::

    uv run python scripts/ecosystem_reporecon_diff.py [--min-stars 5] [--file F]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

URL = "https://devbisme.github.io/RepoRecon/kicad_ai.json"


def missing_repos(index: list[dict], tracked: set[str], min_stars: int = 5) -> list[dict]:
    """Return index entries with ``stars >= min_stars`` whose slug is untracked.

    ``tracked`` holds lowercase ``owner/repo`` slugs. Result is sorted by stars,
    highest first.
    """
    found = [
        r
        for r in index
        if r.get("stars", 0) >= min_stars and f"{r['owner']}/{r['repo']}".lower() not in tracked
    ]
    return sorted(found, key=lambda r: -r["stars"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-stars", type=int, default=5)
    ap.add_argument("--file", type=Path, help="read this JSON instead of fetching")
    args = ap.parse_args(argv)

    from kicad_tools.ecosystem import load_registry

    registry = load_registry()
    tracked = {p.slug.lower() for p in registry.projects if p.slug}
    if args.file:
        index = json.loads(args.file.read_text())
    else:
        with urllib.request.urlopen(URL, timeout=60) as resp:  # noqa: S310
            index = json.load(resp)
    for r in missing_repos(index, tracked, args.min_stars):
        print(f"{r['owner']}/{r['repo']}\t{r['stars']}\t{r['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
