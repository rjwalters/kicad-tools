"""Connectivity DRC rule.

Detects multi-pad nets that are not fully routed on the PCB.  A net
that has two or more pads but does not connect them all with copper
(traces, vias, or filled zones) is structurally unmanufacturable -- the
finished board would have one or more pads literally disconnected from
the rest of their net.

This rule fills the gap noted in Issue #3041: previously, ``kct check``
on a partially-routed PCB reported ``DRC PASS`` because no active rule
cross-referenced the netlist against actual copper connectivity.  Board
00 (simple-led) and board 01 (voltage-divider) both routed only 1/2
nets but still passed DRC, misleading agents into believing the boards
were manufacturable.

Pour-net suppression
--------------------

Power and ground pour nets (``GND``, ``+3V3``, ``VCC``, etc.) are
intentionally satisfied by copper zones rather than traces.  A multi-
pad pour net with copper zones covering its pads is fully connected
even without any trace segments.  This rule defers to
:class:`NetStatusAnalyzer`, which already handles pour-zone connectivity
correctly: pads inside filled-polygon coverage of a same-net zone are
treated as connected, regardless of whether traces touch them.

A pour-named net **without** copper zones is still reported -- the net
is routable as traces, the missing zone is itself the bug, and a
multi-pad pour-named net with no copper at all is structurally just as
disconnected as any signal net.  Use ``--skip connectivity`` to suppress
the rule entirely (e.g., for in-progress partial-route demos).

Severity
--------

Errors (severity ``error``).  An unrouted pad is not manufacturable;
agents need a hard signal that the build is incomplete.  The existing
``--skip connectivity`` flag is the escape hatch for intentional
partial-route fixtures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


class ConnectivityRule(DRCRule):
    """Flag multi-pad nets that are not fully routed.

    Loads the netlist via
    :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` and
    emits one error per net whose ``status`` is ``"incomplete"`` or
    ``"unrouted"``.  Single-pad nets are silently allowed (they are
    handled by :class:`~kicad_tools.validate.rules.single_pad_net.SinglePadNetRule`,
    which categorizes them as NC / connector-NC / defect with the
    appropriate severity).

    The rule consumes the same connectivity model used by ``kct fleet
    status``'s :func:`_compute_routing`, so fleet status and ``kct
    check`` agree on which nets count as "complete".
    """

    rule_id = "connectivity"
    name = "Net Connectivity"
    description = "Detects multi-pad nets that are not fully connected by traces, vias, or zones"

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check the PCB for unrouted multi-pad nets.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules (unused by this rule but required
                by the base-class interface).

        Returns:
            DRCResults containing one error per incomplete / unrouted
            multi-pad net.
        """
        from kicad_tools.analysis.net_status import NetStatusAnalyzer

        results = DRCResults()
        results.rules_checked = 1
        results.rules_checked_by_rule[self.rule_id] = 1

        analyzer = NetStatusAnalyzer(pcb)
        analysis = analyzer.analyze()

        for net_status in analysis.nets:
            # Single-pad nets are handled by SinglePadNetRule; skip
            # them here so the two rules stay orthogonal.  Zero-pad
            # nets (declared headers with no pad assignments) are
            # likewise out of scope -- the NetlistRule covers netlist
            # integrity issues on the header side.
            if net_status.total_pads < 2:
                continue

            if net_status.status == "complete":
                continue

            # Pour/plane nets whose incomplete status is a stitching residual
            # (a thermal-relief cutout or a discontinuous fill island) are
            # advisory, not a missing-trace defect (Issue #3914).  The false
            # positive this fixes: a GND pour net that owns filled copper
            # zones, reported "partially routed" here even though kicad-cli
            # reports 0 unconnected on the same file (boards 03/04/06).
            #
            # The suppression is gated on ``has_filled_zone`` (the net owns a
            # zone that produced REAL fill copper), NOT on the name-based
            # ``net_type`` heuristic: a pour-NAMED net with no zone -- or a
            # zone with fill disabled / zero filled polygons -- is genuinely
            # disconnected and still fires (``test_pour_named_net_without_zone_fires``
            # and the zero-fill edge case).  ``is_advisory_incomplete`` already
            # requires ``status == "incomplete"``, so a pour net that is fully
            # ``unrouted`` (no copper at all) is not suppressed either.
            if net_status.has_filled_zone and net_status.is_advisory_incomplete:
                continue

            # Choose a representative location: the first unconnected
            # pad (sorted alphabetically by REF.PAD inside
            # NetStatusAnalyzer) gives a stable, reproducible coordinate
            # for downstream consumers (CI diffs, fix-suggestion tools).
            if net_status.unconnected_pads:
                pad = net_status.unconnected_pads[0]
                location: tuple[float, float] | None = pad.position
                items: tuple[str, ...] = (pad.full_name,)
            else:
                # Degenerate case: status != "complete" but no pads in
                # unconnected_pads.  This happens for total_pads == 0
                # (caught above) or an unrouted net where the entire
                # ``connected_pads`` partition is empty (all pads land
                # in ``unconnected_pads``).  Defensive fallback to
                # any-pad coordinates.
                location = None
                items = ()

            if net_status.status == "unrouted":
                message = (
                    f"Net '{net_status.net_name}' is unrouted: "
                    f"{net_status.total_pads} pads with no connecting copper "
                    f"(0/{net_status.total_pads} connected)"
                )
            else:
                # "incomplete": at least one pad on the main island,
                # but at least one stranded.
                message = (
                    f"Net '{net_status.net_name}' is partially routed: "
                    f"{net_status.unconnected_count} of {net_status.total_pads} "
                    f"pads stranded (connected island has "
                    f"{net_status.connected_count} pads)"
                )

            results.add(
                DRCViolation(
                    rule_id=self.rule_id,
                    severity="error",
                    message=message,
                    location=location,
                    items=items,
                    nets=(net_status.net_name,),
                )
            )

        return results
