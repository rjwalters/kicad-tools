#!/usr/bin/env python3
"""Parse the chorus strict/partial/unrouted/DRC counts from a route_chorus.py log.

Issue #3873 (MEASUREMENT-INFRASTRUCTURE): the chorus M2/M3 measurement
workflow (``.github/workflows/chorus-flag-matrix.yml``) runs
``scripts/route_chorus.py`` under four flag variants on CI-parity
hardware and needs to extract the headline connectivity numbers from
each leg's captured stdout so the summary job can print a comparison
table.

``route_chorus.py`` ends each run with a "Final chorus report" block::

    ============================================================
    Final chorus report
    ============================================================
      Partially-routed signal nets: 20
        - SPI_SCK
        - ...
      Unrouted signal nets: 0
      Non-connectivity DRC errors: 7

      Routed board: /tmp/chorus_routed_r2.kicad_pcb
      Manufacturable bar: unfinished=20, blocking DRC=7 (target 0/0)

The chorus v21 fixture has a fixed total of 51 multi-pad signal nets
(``CHORUS_V21_NETS_TOTAL`` in ``tests/test_chorus_reach_floor_3237.py``),
so the **strict** (fully-routed) count is::

    strict = total - partial - unrouted

This helper is deliberately tiny and pure (no I/O beyond an optional CLI
front-end) so it can be unit-tested locally against a captured sample
log -- the routing itself is only exercisable on CI-parity hardware with
the private chorus fixture staged.

Usage (CLI, used by the workflow)::

    python scripts/ci/parse_chorus_result.py \\
        --variant m2 --log route_m2.log --output result_m2.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

#: Total multi-pad signal nets in the chorus-test-revA_v21_stripped
#: fixture.  Mirrors ``CHORUS_V21_NETS_TOTAL`` in
#: ``tests/test_chorus_reach_floor_3237.py``.  Strict reach is the
#: complement of the partial+unrouted count against this total.
CHORUS_V21_NETS_TOTAL = 51

_PARTIAL_RE = re.compile(r"^\s*Partially-routed signal nets:\s*(\d+)\s*$", re.MULTILINE)
_UNROUTED_RE = re.compile(r"^\s*Unrouted signal nets:\s*(\d+)\s*$", re.MULTILINE)
_DRC_RE = re.compile(r"^\s*Non-connectivity DRC errors:\s*(\d+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ChorusResult:
    """Headline connectivity numbers extracted from a route_chorus log."""

    variant: str
    total: int
    partial: int
    unrouted: int
    strict: int
    drc_errors: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _find_last_int(pattern: re.Pattern[str], text: str, label: str) -> int:
    """Return the integer from the LAST match of ``pattern`` in ``text``.

    route_chorus.py can print the per-class report more than once across
    its stages; the authoritative "Final chorus report" is the last one,
    so we always take the final match.
    """
    matches = pattern.findall(text)
    if not matches:
        raise ValueError(
            f"could not find {label!r} count in route_chorus.py log (pattern: {pattern.pattern!r})"
        )
    return int(matches[-1])


def parse_chorus_log(
    log_text: str,
    *,
    variant: str = "",
    total: int = CHORUS_V21_NETS_TOTAL,
) -> ChorusResult:
    """Extract strict/partial/unrouted/DRC counts from a route_chorus log.

    Parameters
    ----------
    log_text:
        The full captured stdout of a ``scripts/route_chorus.py`` run.
    variant:
        Free-form label for the flag variant (e.g. ``"baseline"``,
        ``"m2"``, ``"m3"``, ``"m2m3"``); echoed into the result.
    total:
        Total multi-pad signal nets on the board.  Defaults to the
        chorus v21 fixture's ``CHORUS_V21_NETS_TOTAL``.

    Returns
    -------
    ChorusResult
        ``strict = total - partial - unrouted``.

    Raises
    ------
    ValueError
        If any of the three report lines is absent (a routing run that
        crashed before printing its final report).
    """
    partial = _find_last_int(_PARTIAL_RE, log_text, "Partially-routed signal nets")
    unrouted = _find_last_int(_UNROUTED_RE, log_text, "Unrouted signal nets")
    drc_errors = _find_last_int(_DRC_RE, log_text, "Non-connectivity DRC errors")
    strict = total - partial - unrouted
    return ChorusResult(
        variant=variant,
        total=total,
        partial=partial,
        unrouted=unrouted,
        strict=strict,
        drc_errors=drc_errors,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant",
        default="",
        help="Flag-variant label echoed into the JSON (baseline/m2/m3/m2m3).",
    )
    parser.add_argument(
        "--log",
        type=Path,
        required=True,
        help="Path to the captured route_chorus.py stdout log.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the parsed result as JSON to this path (also prints to stdout).",
    )
    parser.add_argument(
        "--total",
        type=int,
        default=CHORUS_V21_NETS_TOTAL,
        help=f"Total signal nets (default {CHORUS_V21_NETS_TOTAL}).",
    )
    args = parser.parse_args(argv)

    log_text = args.log.read_text(encoding="utf-8", errors="replace")
    result = parse_chorus_log(log_text, variant=args.variant, total=args.total)
    payload = json.dumps(result.as_dict(), indent=2)
    if args.output is not None:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
