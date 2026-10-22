#!/usr/bin/env python3
"""Board 07 ``--differential-pairs`` baseline ratchet (issue #7795).

Reads a ``kicad-cli pcb drc --format json`` report for board 07 regenerated
with ``generate_design.py --differential-pairs`` and gates three metrics
against the committed ``.github/board07-diffpair-baseline.yml``:

* ``copper_safety`` -- clearance / shorting / crossing / hole / edge errors
  (same categories as ``check_copper_safety_drc.py``).
* ``opens`` -- ``unconnected_items`` entries (nets the coupled path stranded).
* ``skew_out_of_range`` -- match-group skew violations (DQS skew included).

Every metric is a ratchet: the count may not exceed its baseline, and the
baseline file may not be raised relative to ``--previous-baseline-file`` (the
file as it stood before its last change).  31/31 reach and 0 opens are the
long-term targets (#5333, #7619, #7769); the day-one gate is the measured
baseline.  A metric whose baseline is ``null`` has not been measured yet: it is
reported, never gated, and the notice says what to commit.

A missing, empty, unparsable or ``violations``-less report is a tool failure,
never "0 errors" (#6724).  Runs in ``slow-tests.yml``, not PR CI (#6738).

Exit codes:
    0 -- all metrics within baseline.
    1 -- tool failure (bad report or baseline file).
    2 -- a metric exceeds its baseline.
    3 -- the committed baseline was raised above ``--previous-baseline-file``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml

_COPPER = Path(__file__).resolve().with_name("check_copper_safety_drc.py")
_spec = importlib.util.spec_from_file_location("check_copper_safety_drc", _COPPER)
copper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(copper)

ReportError = copper.ReportError
METRICS = ("copper_safety", "opens", "skew_out_of_range")
DEFAULT_BASELINE_FILE = Path(".github/board07-diffpair-baseline.yml")


def load_unconnected(path: Path) -> list[dict[str, Any]]:
    """Return the report's ``unconnected_items`` (absent key means none)."""
    data = json.loads(path.read_text())  # load_report() already validated this
    items = data.get("unconnected_items", [])
    if not isinstance(items, list):
        raise ReportError(f"DRC report 'unconnected_items' is not a list: {path}")
    return items


def load_baseline(path: Path) -> dict[str, int | None]:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ReportError(f"baseline file unreadable: {path}: {exc}") from exc
    baseline = data.get("baseline") if isinstance(data, dict) else None
    if not isinstance(baseline, dict):
        raise ReportError(f"baseline file has no 'baseline' mapping: {path}")
    out: dict[str, int | None] = {}
    for key in METRICS:
        value = baseline.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ReportError(f"baseline '{key}' must be null or a non-negative int in {path}")
        out[key] = value
    return out


def measure(report: Path) -> dict[str, int]:
    violations = copper.load_report(report)
    return {
        "copper_safety": len(copper.copper_safety_violations(violations)),
        "opens": len(load_unconnected(report)),
        "skew_out_of_range": sum(1 for v in violations if v.get("type") == "skew_out_of_range"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path, help="kicad-cli DRC JSON report")
    parser.add_argument("--baseline-file", type=Path, default=DEFAULT_BASELINE_FILE)
    parser.add_argument(
        "--previous-baseline-file",
        type=Path,
        default=None,
        help="baseline file before its last change; the committed baseline may not exceed it",
    )
    args = parser.parse_args(argv)

    try:
        counts = measure(args.report)
        baseline = load_baseline(args.baseline_file)
        previous = (
            load_baseline(args.previous_baseline_file)
            if args.previous_baseline_file is not None
            else None
        )
    except ReportError as exc:
        print(f"::error::{exc}")
        return 1

    if previous is not None:
        for key in METRICS:
            old, new = previous[key], baseline[key]
            if old is not None and (new is None or new > old):
                shown = "removed" if new is None else f"raised from {old} to {new}"
                print(f"::error::baseline '{key}' was {shown}; baselines may only go down")
                return 3

    exit_code = 0
    for key in METRICS:
        limit = baseline[key]
        if limit is None:
            print(f"::notice::{key}: {counts[key]} (no baseline yet; commit this value)")
        elif counts[key] > limit:
            print(f"::error::{key}: {counts[key]} exceeds baseline {limit}")
            exit_code = 2
        else:
            print(f"{key}: {counts[key]} (baseline {limit})")
            if counts[key] < limit:
                print(f"::notice::{key} below baseline; lower it to {counts[key]}")
    for v in violations_of_interest(args.report):
        print(f"  {v}")
    return exit_code


def violations_of_interest(report: Path) -> list[str]:
    """Skew violation text (carries the measured mm) for the job summary."""
    violations = copper.load_report(report)
    return [copper.describe(v) for v in violations if v.get("type") == "skew_out_of_range"]


if __name__ == "__main__":
    sys.exit(main())
