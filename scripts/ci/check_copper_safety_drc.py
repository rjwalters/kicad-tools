#!/usr/bin/env python3
"""Copper-safety DRC ratchet for a freshly regenerated board (issue #6738).

Reads a ``kicad-cli pcb drc --format json`` report, counts violations in the
copper-safety categories and fails when the count exceeds a committed floor
(``.github/copper-safety-drc-floor.yml``).  Matching-quality residuals such as
``skew_out_of_range`` are deliberately not counted.

A missing, empty, unparsable or ``violations``-less report is a tool failure,
never "0 errors" (a killed kicad-cli must not read as clean, #6724).

Exit codes:
    0 -- count within floor.
    1 -- tool failure (bad report, bad floor file).
    2 -- count exceeds the floor.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

COPPER_SAFETY_TYPES = frozenset(
    {
        "clearance",
        "shorting_items",
        "tracks_crossing",
        "hole_clearance",
        "copper_edge_clearance",
    }
)
DEFAULT_FLOOR_FILE = Path(".github/copper-safety-drc-floor.yml")


class ReportError(Exception):
    """The DRC report or floor file cannot be trusted."""


def load_report(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text()
    except OSError as exc:
        raise ReportError(f"DRC report not readable: {path}: {exc}") from exc
    if not text.strip():
        raise ReportError(f"DRC report is empty: {path}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReportError(f"DRC report is not valid JSON (truncated?): {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("violations"), list):
        raise ReportError(f"DRC report has no 'violations' list: {path}")
    return data["violations"]


def load_floor(path: Path, key: str) -> int:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ReportError(f"floor file unreadable: {path}: {exc}") from exc
    floors = data.get("floors") if isinstance(data, dict) else None
    value = floors.get(key) if isinstance(floors, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReportError(f"floor '{key}' missing or not a non-negative int in {path}")
    return value


def copper_safety_violations(violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [v for v in violations if v.get("type") in COPPER_SAFETY_TYPES]


def describe(violation: dict[str, Any]) -> str:
    items = violation.get("items") or []
    parts = []
    for item in items:
        pos = item.get("pos") or {}
        parts.append(f"{item.get('description', '?')} @ ({pos.get('x', '?')}, {pos.get('y', '?')})")
    return f"{violation.get('type')}: {violation.get('description', '')} | " + " ; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path, help="kicad-cli DRC JSON report")
    parser.add_argument("--floor-file", type=Path, default=DEFAULT_FLOOR_FILE)
    parser.add_argument("--floor-key", default="board-07-fresh-regen")
    args = parser.parse_args(argv)

    try:
        violations = load_report(args.report)
        floor = load_floor(args.floor_file, args.floor_key)
    except ReportError as exc:
        print(f"::error::{exc}")
        return 1

    found = copper_safety_violations(violations)
    for v in found:
        print(f"  {describe(v)}")
    print(f"copper-safety violations: {len(found)} (floor {floor})")
    if len(found) > floor:
        print(f"::error::copper-safety DRC errors {len(found)} exceed floor {floor}")
        return 2
    if len(found) < floor:
        print(f"::notice::count below floor; lower '{args.floor_key}' to {len(found)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
