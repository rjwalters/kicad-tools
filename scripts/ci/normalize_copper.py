#!/usr/bin/env python3
"""Routed-copper normalizer for the determinism gates (issue #5580).

``scripts/ci/board_route_determinism_smoke.sh`` routes a board N times and
asserts the routed COPPER -- the ``(segment ...)`` / ``(via ...)`` /
``(arc ...)`` set -- is byte-identical across runs (#3799 / #3894).  Its
original ``normalize_copper()`` was line-based::

    sed -E 's/\\(uuid "[^"]*"\\)/(uuid "X")/g; ...' "$1" \\
      | grep -E '^[[:space:]]*\\((segment|via|arc)' \\
      | sort

This repo writes copper as MULTI-LINE s-expressions, so that ``grep`` kept
only the bare ``(segment`` / ``(via`` HEADER lines and discarded every
``(start ...)`` / ``(end ...)`` / ``(width ...)`` / ``(layer ...)`` /
``(net ...)`` child.  After ``sort`` the "normalized copper" was a run of
identical ``(segment`` lines followed by identical ``(via`` lines, i.e. the
gate compared ``segment_count == segment_count && via_count == via_count``
and was blind to geometry: two routes placing every trace on a different
path, layer or width compared EQUAL.  Measured on board 03 before this fix,
a routed PCB reduced to 1793 lines holding 3 distinct values (2026-09-19 at
0c261d41; #5580 measured 1913 lines = 1872 segments + 41 vias on an earlier
route -- the absolute counts drift with the router, the degeneracy does not).

This helper compares whole, paren-balanced nodes instead.  It mirrors the
two in-repo reference implementations --
``tests/router/test_board_route_determinism.py::_normalize_copper`` and
``tests/test_routing_plan_5510.py::_copper_elements`` -- using the repo's
own s-expression parser so quoting and nesting are handled correctly rather
than by counting parentheses.

What is compared:
    * TOP-LEVEL ``(segment ...)`` / ``(via ...)`` / ``(arc ...)`` nodes only.
      A copper-shaped node nested inside a ``(footprint ...)`` is pad/graphic
      geometry, not routed copper.
    * Every child field of those nodes (start, end, mid, width, layer(s),
      net, ...), so a moved, re-layered, re-widened or re-netted trace is a
      DIFFERENCE.
    * ``uuid`` / ``tstamp`` children are DROPPED.  The router emits
      deterministic UUIDs when seeded, but they are stripped defensively so a
      regression in that toggle cannot masquerade as a copper divergence.
    * The record list is SORTED, so emission ORDER does not matter -- only
      the multiset of copper geometry.  Multiplicity IS preserved.
    * Zone pour fills are EXCLUDED (#5578): pours are filled after routing,
      flow around finished traces, and are separately nondeterministic.

Output: one record per line on stdout, sorted -- so a caller can compare two
files with a plain line-based ``diff`` and get a readable, per-element diff.

Exit codes (mirrors the sibling asserters' convention, e.g.
``check_copper_lvs.py``):
    0 -- normalized copper written to stdout.
    1 -- usage / IO / parse error (missing file, malformed s-expression).
    2 -- no routed copper found.  Two empty outputs compare equal, so a
         normalization regression that filtered everything out would report
         determinism it never measured; this makes that loud.  Pass
         ``--allow-empty`` when an empty result is legitimate (e.g.
         normalizing an UNROUTED board).

Usage:
    scripts/ci/normalize_copper.py <pcb> [--allow-empty]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Top-level node tags that carry routed copper.
COPPER_TAGS = ("segment", "via", "arc")

#: Per-element identity children stripped before comparison.
IDENTITY_TAGS = ("uuid", "tstamp")


def normalize_copper(pcb_text: str) -> list[str]:
    """Return *pcb_text*'s routed copper as a sorted list of one-line records.

    Args:
        pcb_text: Full ``.kicad_pcb`` file contents.

    Returns:
        Sorted list of compact, identity-stripped s-expression strings, one
        per top-level ``(segment ...)`` / ``(via ...)`` / ``(arc ...)`` node.
        Each record is a single line (no embedded newlines) so the caller can
        diff two normalizations line by line.

    Raises:
        ValueError: *pcb_text* is not a parseable s-expression.
    """
    # Imported lazily so ``--help`` and the argument/IO errors above still
    # work when the package environment is unavailable.
    from kicad_tools.sexp import parse_string

    # The wrapper makes a bare fragment and a full ``(kicad_pcb ...)``
    # document parse the same way.
    wrapper = parse_string("(normalization " + pcb_text + ")")
    root = wrapper.find_child("kicad_pcb") or wrapper

    records: list[str] = []
    for node in root.children:
        if node.is_atom or node.name not in COPPER_TAGS:
            continue
        for key in IDENTITY_TAGS:
            for identity in node.find_children(key):
                node.children.remove(identity)
        records.append(node.to_string(compact=True))
    return sorted(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalize a routed PCB's copper to a sorted whole-node record list.",
    )
    parser.add_argument("pcb", type=Path, help="Path to a .kicad_pcb file")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit 0 instead of 2 when the PCB contains no routed copper.",
    )
    args = parser.parse_args(argv)

    pcb: Path = args.pcb
    if not pcb.is_file():
        print(f"ERROR: PCB not found: {pcb}", file=sys.stderr)
        return 1

    try:
        text = pcb.read_text()
    except OSError as exc:  # pragma: no cover - unreadable file
        print(f"ERROR: cannot read {pcb}: {exc}", file=sys.stderr)
        return 1

    try:
        records = normalize_copper(text)
    except Exception as exc:
        print(f"ERROR: cannot parse copper from {pcb}: {exc}", file=sys.stderr)
        return 1

    if not records and not args.allow_empty:
        print(
            f"ERROR: no routed copper (segment/via/arc) found in {pcb}. "
            "Refusing to emit an empty normalization, which would compare "
            "equal to any other empty one (pass --allow-empty if intended).",
            file=sys.stderr,
        )
        return 2

    sys.stdout.write("".join(record + "\n" for record in records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
