"""Group 18 -- ``kct check``'s ``ClearanceRule``, the post-route gate.

Unlike the four router-side adapters, this consumer is a *whole-board* check:
it takes a parsed ``PCB`` and the same ``manufacturers.base.DesignRules`` that
wrote the board's ``.kicad_pro``, and returns a ``DRCResults`` of
``DRCViolation`` rows that already name their two nets.  So the adapter writes
the case out through the harness's own writer
(:func:`tests.conformance.board.write_case` -- byte-for-byte the board
kicad-cli is measuring), loads it back, and calls::

    ClearanceRule().check(pcb, board.manufacturer_rules(case.rules))

Driving it off the same file the oracle measures is deliberate: it removes
"the two sides were handed different geometry" as an explanation for any
disagreement the row records.

**One scalar, no trace/via split.**  ``ClearanceRule`` reads a single
``design_rules.min_clearance_mm`` for every copper pair on the board.  It has
no separate ``via_clearance``, which is exactly why it cannot reproduce the
insertion-order asymmetry of groups 12/13 -- and why its row is the one to
compare against when asking "what would the shared kernel have to agree with?".

**Zones are out of scope for this row.**  Zone copper is checked by
``SegmentZoneClearanceRule`` / ``ViaZoneClearanceRule``, not by
``ClearanceRule``; but no pair this harness *places* involves the zone net (the
pour exists to exercise the refill path, and the report compares against
``OracleResult.without_zones()``), so including those two rules could not
change a single compared cell.  They are left out rather than run for show.

**Pad geometry is exact here.**  ``ClearanceRule`` models a pad by its real
polygon outline, which is the other half of the ``roundrect-corner-gap``
fixture: the same 0.22 mm corner gap that the C++ grid's rectangle bounds call
0.1164 mm is measured correctly by this consumer, and it agrees with kicad-cli.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.conformance.adapters import (
    KIND_CLEARANCE,
    KIND_HOLE_CLEARANCE,
    KIND_HOLE_TO_HOLE,
    Verdict,
)
from tests.conformance.adapters._support import ALL_PAIR_KINDS
from tests.conformance.generator import CopperCase

__all__ = ["KctCheckAdapter"]

# ``rule_id`` prefixes -> the harness's canonical verdict kind.  Longest match
# wins, so the hole rules are listed before the plain clearance family.
_RULE_ID_KINDS: tuple[tuple[str, str], ...] = (
    ("clearance_hole_to_hole", KIND_HOLE_TO_HOLE),
    ("hole_to_hole", KIND_HOLE_TO_HOLE),
    ("clearance_pth_hole", KIND_HOLE_CLEARANCE),
    ("hole_clearance", KIND_HOLE_CLEARANCE),
    ("clearance", KIND_CLEARANCE),
)


class KctCheckAdapter:
    """Drives ``kicad_tools.validate.rules.clearance.ClearanceRule``."""

    name = "kct_check"
    group = 18
    pair_kinds = ALL_PAIR_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.clearance import ClearanceRule
        from tests.conformance.board import manufacturer_rules, write_case

        with tempfile.TemporaryDirectory(prefix="conformance_kct_check_") as tmp:
            board = write_case(case, Path(tmp))
            pcb = PCB.load(board.pcb_path)
            results = ClearanceRule().check(pcb, manufacturer_rules(case.rules))

        found: set[Verdict] = set()
        for violation in results.violations:
            nets = [name for name in (violation.nets or ()) if name]
            if len(nets) < 2 or nets[0] == nets[1]:
                # Same-net or net-less findings are not a foreign-net pair and
                # have no two-net key in this harness's vocabulary.
                continue
            found.add(
                Verdict.pair(
                    _kind_for(violation.rule_id),
                    nets[0],
                    nets[1],
                    gap_mm=violation.actual_value,
                    required_mm=violation.required_value,
                )
            )
        return found


def _kind_for(rule_id: str) -> str:
    for prefix, kind in _RULE_ID_KINDS:
        if rule_id.startswith(prefix):
            return kind
    return KIND_CLEARANCE
