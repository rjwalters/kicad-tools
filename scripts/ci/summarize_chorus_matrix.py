#!/usr/bin/env python3
"""Render the chorus M2/M3 flag-matrix comparison table (issue #3873).

The summary job of ``.github/workflows/chorus-flag-matrix.yml`` collects
the four per-leg ``result_<variant>.json`` files written by
``scripts/ci/parse_chorus_result.py`` and renders a single Markdown
comparison table (baseline vs m2 vs m3 vs m2m3, with strict-count deltas
relative to baseline) to the workflow Step Summary, so the headline
result is visible without digging through individual leg logs.

The table is the deliverable of the measurement harness: the whole point
of #3873 is to read, in one place, "did M2/M3 actually move the chorus
strict count, and by how much" on hardware that the dev machine could not
provide.

Usage::

    python scripts/ci/summarize_chorus_matrix.py --results-dir chorus-artifacts
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: Canonical leg order for the table (baseline first so deltas read left
#: to right against it).
VARIANT_ORDER = ("baseline", "m2", "m3", "m2m3")

#: Human-readable flag description per variant, for the table.
VARIANT_FLAGS = {
    "baseline": "(none)",
    "m2": "--joint-region-resolve",
    "m3": "--placement-nudge",
    "m2m3": "--joint-region-resolve --placement-nudge",
}


def load_results(results_dir: Path) -> dict[str, dict[str, object]]:
    """Load every ``result_*.json`` under ``results_dir`` keyed by variant."""
    results: dict[str, dict[str, object]] = {}
    for path in sorted(results_dir.rglob("result_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        variant = str(data.get("variant") or path.stem.removeprefix("result_"))
        results[variant] = data
    return results


def _delta_str(value: int | None, baseline: int | None) -> str:
    """Signed delta of ``value`` vs ``baseline`` strict count, or ``n/a``."""
    if value is None or baseline is None:
        return "n/a"
    delta = value - baseline
    return f"+{delta}" if delta > 0 else str(delta)


def render_table(results: dict[str, dict[str, object]]) -> str:
    """Render the Markdown comparison table from loaded per-leg results."""
    lines: list[str] = []
    lines.append("## Chorus M2/M3 flag-matrix results")
    lines.append("")
    lines.append(
        "Strict = fully-routed signal nets out of the chorus v21 total "
        "(51).  Delta is vs the baseline leg."
    )
    lines.append("")

    if not results:
        lines.append(
            "_No leg results found.  Either the chorus fixture was absent "
            "(no CHORUS_TEST_GIT_URL secret) so every leg skipped, or no "
            "leg emitted a parseable final report.  See the per-leg logs._"
        )
        return "\n".join(lines) + "\n"

    baseline = results.get("baseline")
    baseline_strict: int | None = None
    if baseline is not None:
        baseline_value = baseline.get("strict")
        if isinstance(baseline_value, int):
            baseline_strict = baseline_value

    lines.append("| variant | flags | strict | delta | partial | unrouted | DRC errors |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")

    # Stable, known order first; then any unexpected extra variants.
    ordered = [v for v in VARIANT_ORDER if v in results]
    ordered += [v for v in results if v not in VARIANT_ORDER]

    for variant in ordered:
        data = results[variant]
        strict = data.get("strict")
        strict_int = int(strict) if isinstance(strict, int) else None
        flags = VARIANT_FLAGS.get(variant, "(unknown)")
        delta = _delta_str(strict_int, baseline_strict) if variant != "baseline" else "-"
        lines.append(
            f"| {variant} "
            f"| `{flags}` "
            f"| {data.get('strict', '?')} "
            f"| {delta} "
            f"| {data.get('partial', '?')} "
            f"| {data.get('unrouted', '?')} "
            f"| {data.get('drc_errors', '?')} |"
        )

    lines.append("")
    lines.append(
        "_This is the measurement harness output (issue #3873).  "
        "Tightening tests/test_chorus_reach_floor_3237.py or changing "
        "route_chorus.py defaults are follow-ups gated on these numbers._"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        type=Path,
        required=True,
        help="Directory containing the per-leg result_<variant>.json files.",
    )
    args = parser.parse_args(argv)
    results = load_results(args.results_dir)
    sys.stdout.write(render_table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
