"""Group 18 -- ``kct check``'s clearance family, the post-route gate.

Unlike the router-side adapters, this consumer is a *whole-board* check: it
takes a parsed ``PCB`` and the same ``manufacturers.base.DesignRules`` that
wrote the board's ``.kicad_pro``, and returns ``DRCViolation`` rows that
already name their two nets.  So the adapter writes the case out through the
harness's own writer (:func:`tests.conformance.board.write_case` -- byte-for-byte
the board kicad-cli is measuring), loads it back, and drives the rules the way
``validate/checker.py`` registers them::

    ClearanceRule().check(pcb, rules)              # copper vs copper
    EdgeClearanceRule().check(pcb, rules)          # copper vs Edge.Cuts
    SegmentZoneClearanceRule().check(pcb, rules)   # track vs zone fill
    ViaZoneClearanceRule().check(pcb, rules)       # via/pad vs zone fill
    check_physical_copper_gap(pcb, min_clearance)  # net-independent slits

Driving them off the same file the oracle measures is deliberate: it removes
"the two sides were handed different geometry" as an explanation for any
disagreement the row records.

**One scalar, no trace/via split.**  ``ClearanceRule`` reads a single
``design_rules.min_clearance_mm`` for every copper pair on the board.  It has
no separate ``via_clearance``, which is exactly why it cannot reproduce the
insertion-order asymmetry of groups 12/13 -- and why its row is the one to
compare against when asking "what would the shared kernel have to agree with?".

**Zone rows require a refilled board, so this adapter refills (#5644).**
``SegmentZoneClearanceRule`` / ``ViaZoneClearanceRule`` / ``physical_gap.py``
all read committed ``filled_polygon`` copper -- this repo's source of truth
(#3482/#3523/#3527) -- and the harness writes zones *unfilled*.  Scoring them
against an unfilled pour would report a confident zero for three of the five
entry points above.  So a case that carries a pour is refilled through
``kicad-cli pcb drc --refill-zones`` (the same wrapper the oracle uses) before
the rules run, on the adapter's own scratch copy, and this row's
:meth:`~KctCheckAdapter.available` therefore requires kicad-cli.  A case with
no pour is measured as-is: there is nothing to fill, and the three zone-aware
entry points return empty.

A fresh fill is backed off from foreign copper by the applied clearance, so a
zone pair can only ever be placed on the clean side of the threshold (see
``generator.py``'s module docstring).  These rows can therefore show
over-rejection and cannot show under-rejection -- which is still the whole
question for a rule that reads *filled* geometry: does it agree that copper
20-60 um clear of the fill is clear?

**Pad geometry is exact here.**  ``ClearanceRule`` models a pad by its real
polygon outline, which is the other half of the ``roundrect-corner-gap``
fixture: the same 0.22 mm corner gap that the C++ grid's rectangle bounds call
0.1164 mm is measured correctly by this consumer, and it agrees with kicad-cli.

**Edge findings are keyed back to a net by number.**  ``EdgeClearanceRule``
populates ``items=("Net <n>",)`` and leaves ``DRCViolation.nets`` empty, so
the adapter resolves ``<n>`` through the loaded board's own net table rather
than guessing from geometry.  The partner is the reserved
:data:`~tests.conformance.adapters.BOARD_EDGE` pseudo-net, which is what the
oracle pairs a one-sided ``copper_edge_clearance`` row with.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from tests.conformance.adapters import (
    BOARD_EDGE,
    KIND_CLEARANCE,
    KIND_COPPER_EDGE,
    KIND_HOLE_CLEARANCE,
    KIND_HOLE_TO_HOLE,
    Verdict,
)
from tests.conformance.adapters._support import ALL_PAIR_KINDS
from tests.conformance.generator import CopperCase, PairKind

__all__ = ["KCT_CHECK_PAIR_KINDS", "KctCheckAdapter"]

#: Every routing kind, plus the #5644 zone and edge kinds this row now drives
#: a rule for.  ``pad-pad`` stays out: it is placement copper with no routing
#: candidate, and group 19 owns it.
KCT_CHECK_PAIR_KINDS = ALL_PAIR_KINDS | frozenset({*PairKind.ZONE, *PairKind.EDGE})

# ``rule_id`` prefixes -> the harness's canonical verdict kind.  Longest match
# wins, so the hole rules are listed before the plain clearance family.
_RULE_ID_KINDS: tuple[tuple[str, str], ...] = (
    ("clearance_hole_to_hole", KIND_HOLE_TO_HOLE),
    ("hole_to_hole", KIND_HOLE_TO_HOLE),
    ("clearance_pth_hole", KIND_HOLE_CLEARANCE),
    ("hole_clearance", KIND_HOLE_CLEARANCE),
    ("edge_clearance", KIND_COPPER_EDGE),
    ("clearance", KIND_CLEARANCE),
)

# ``rule_id`` prefixes whose findings are about zone-fill copper.  Recorded on
# the verdict so the report can keep zone rows to refilled runs.
_ZONE_RULE_PREFIXES = ("clearance_segment_zone", "clearance_via_zone", "clearance_pad_zone")

#: ``EdgeClearanceRule``'s item format; see the module docstring.
_NET_ITEM_RE = re.compile(r"^Net\s+(\d+)$")


class KctCheckAdapter:
    """Drives ``kct check``'s five clearance-family entry points."""

    name = "kct_check"
    group = 18
    pair_kinds = KCT_CHECK_PAIR_KINDS

    def available(self) -> bool:
        # The zone rules read *filled* polygons, and only kicad-cli can fill
        # them.  Reporting a row measured without the fill would be a
        # confident zero for three of the five entry points, so the row goes
        # ``not measured`` instead -- the same rule the oracle applies to a
        # missing kicad-cli.
        from tests.conformance.oracle import kicad_cli_available

        return kicad_cli_available()

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        from kicad_tools.cli.runner import run_refill_zones
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.clearance import (
            ClearanceRule,
            SegmentZoneClearanceRule,
            ViaZoneClearanceRule,
        )
        from kicad_tools.validate.rules.edge import EdgeClearanceRule
        from kicad_tools.validate.rules.physical_gap import check_physical_copper_gap
        from tests.conformance.board import manufacturer_rules, write_case

        rules = manufacturer_rules(case.rules)
        with tempfile.TemporaryDirectory(prefix="conformance_kct_check_") as tmp:
            board = write_case(case, Path(tmp))
            if case.zone is not None:
                fill = run_refill_zones(board.pcb_path)
                if not fill.success:
                    raise RuntimeError(
                        f"kicad-cli could not refill zones on {board.pcb_path}: {fill.stderr}"
                    )
            pcb = PCB.load(board.pcb_path)
            violations = [
                *ClearanceRule().check(pcb, rules).violations,
                *EdgeClearanceRule().check(pcb, rules).violations,
                *SegmentZoneClearanceRule().check(pcb, rules).violations,
                *ViaZoneClearanceRule().check(pcb, rules).violations,
                # Net-independent slit preflight.  The threshold is the same
                # ``min_clearance_mm`` the netclass applies, which is the only
                # value this corpus configures; ``checker.py`` leaves it opt-in
                # (``physical_copper_gap_mm``) precisely because it has no
                # default of its own.
                *check_physical_copper_gap(pcb, case.rules.project_clearance).violations,
            ]
            net_names = {net.number: net.name for net in pcb.nets.values()}

        found: set[Verdict] = set()
        for violation in violations:
            kind = _kind_for(violation.rule_id)
            if kind == KIND_COPPER_EDGE:
                net = _edge_net(violation.items, net_names)
                if net is None:
                    # A zone or pad edge finding whose item carries no net
                    # token: no two-key identity in this harness's vocabulary.
                    continue
                found.add(
                    Verdict.pair(
                        kind,
                        net,
                        BOARD_EDGE,
                        gap_mm=violation.actual_value,
                        required_mm=violation.required_value,
                    )
                )
                continue
            nets = [name for name in (violation.nets or ()) if name]
            if len(nets) < 2 or nets[0] == nets[1]:
                # Same-net or net-less findings are not a foreign-net pair and
                # have no two-net key in this harness's vocabulary.
                continue
            found.add(
                Verdict.pair(
                    kind,
                    nets[0],
                    nets[1],
                    gap_mm=violation.actual_value,
                    required_mm=violation.required_value,
                    zone=violation.rule_id.startswith(_ZONE_RULE_PREFIXES),
                )
            )
        return found


def _kind_for(rule_id: str) -> str:
    for prefix, kind in _RULE_ID_KINDS:
        if rule_id.startswith(prefix):
            return kind
    return KIND_CLEARANCE


def _edge_net(items: tuple[str, ...], net_names: dict[int, str]) -> str | None:
    """The net name an ``EdgeClearanceRule`` finding is about, if it names one."""
    for item in items:
        match = _NET_ITEM_RE.match(item.strip())
        if match is None:
            continue
        name = net_names.get(int(match.group(1)))
        if name:
            return name
    return None
