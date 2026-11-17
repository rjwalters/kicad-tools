#!/usr/bin/env python3
"""Diff the 3D model coverage of two ``kct render --format json`` reports (issue #7689).

Compares a host run against a pinned-Docker run (``KCT_KICAD_DOCKER=1``) of the
same board(s) and exits 1 on any coverage difference: percent, covered/total,
per-class counts, and which footprint references are uncovered or unresolved.
The ``env`` label and model *paths* legitimately differ between hosts and are
ignored; PNG bytes are never compared (renders are not evidence, #7678).

Usage: compare_model_coverage.py host.json pinned.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _extract(report: dict) -> dict[str, dict]:
    """Map board name -> comparable coverage facts."""
    boards: dict[str, dict] = {}
    for entry in report.get("boards", []):
        check = entry.get("model_check", {})
        coverage = {k: v for k, v in check.get("coverage", {}).items() if k != "env"}
        boards[entry.get("board", "?")] = {
            "coverage": coverage,
            "uncovered": sorted(
                (g["reference"], g.get("class", "")) for g in check.get("uncovered_models", [])
            ),
            "unresolved": sorted(m["reference"] for m in check.get("unresolved_models", [])),
        }
    return boards


def compare(host: dict, pinned: dict) -> list[str]:
    """Return human-readable differences (empty when the reports agree)."""
    a, b = _extract(host), _extract(pinned)
    diffs: list[str] = []
    for name in sorted(a.keys() | b.keys()):
        if name not in a or name not in b:
            diffs.append(f"{name}: present only in {'host' if name in a else 'pinned'} report")
            continue
        for key in ("coverage", "uncovered", "unresolved"):
            if a[name][key] != b[name][key]:
                diffs.append(f"{name}: {key} differs: host={a[name][key]} pinned={b[name][key]}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", type=Path, help="kct render --format json from the host")
    parser.add_argument("pinned", type=Path, help="same, run with KCT_KICAD_DOCKER=1")
    args = parser.parse_args(argv)
    try:
        host = json.loads(args.host.read_text())
        pinned = json.loads(args.pinned.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read reports: {exc}", file=sys.stderr)
        return 2
    diffs = compare(host, pinned)
    for line in diffs:
        print(line)
    if diffs:
        return 1
    print(f"model coverage identical across {len(_extract(host))} board(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
