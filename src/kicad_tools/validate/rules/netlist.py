"""Netlist integrity rule for DRC checks.

Detects pads that reference net names not declared in the board-level
net header.  After ``_fixup_net_numbers()`` runs during PCB parsing,
any pad that still has ``net_number == 0`` with a non-empty ``net_name``
is referencing an undeclared net.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


class NetlistRule(DRCRule):
    """Check that every pad net name is declared in the board net header.

    Builds a set of declared net names from ``pcb.nets`` and flags any
    pad whose ``net_name`` is non-empty but absent from that set.
    """

    rule_id = "net_undeclared"
    name = "Undeclared Net"
    description = "Detects pads referencing nets not declared in the board header"

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all footprint pads for undeclared net references.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules (unused by this rule but required
                by the base-class interface).

        Returns:
            DRCResults containing one warning per pad that references an
            undeclared net.
        """
        results = DRCResults()
        results.rules_checked = 1

        # Build the set of declared net names from the board header.
        declared_nets: set[str] = set()
        for net in pcb.nets.values():
            if net.name:
                declared_nets.add(net.name)

        # Scan every footprint pad.
        for fp in pcb.footprints:
            ref = fp.reference or fp.name
            for pad in fp.pads:
                # Skip pads with no net assignment (unconnected).
                if not pad.net_name:
                    continue

                # Skip the special unconnected net (net 0 "").
                # Already handled by the empty-name check above, but
                # be explicit for net_number == 0 with empty name.
                if pad.net_number == 0 and pad.net_name == "":
                    continue

                if pad.net_name not in declared_nets:
                    # Compute absolute pad position (footprint pos + pad
                    # offset rotated by footprint rotation).
                    location = _absolute_pad_position(fp, pad)

                    results.add(
                        DRCViolation(
                            rule_id=self.rule_id,
                            severity="warning",
                            message=(
                                f"Pad {ref}-{pad.number} references undeclared net "
                                f'"{pad.net_name}" on footprint {ref}'
                            ),
                            location=location,
                            items=(f"{ref}-{pad.number}",),
                        )
                    )

        return results


def _absolute_pad_position(fp, pad) -> tuple[float, float]:
    """Compute the absolute board position of a pad.

    Takes into account the footprint's position and rotation.

    Args:
        fp: The parent Footprint.
        pad: The Pad whose position to compute.

    Returns:
        (x, y) tuple in board coordinates.
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    fx, fy = fp.position
    px, py = pad.position

    # Rotate pad offset by footprint rotation (KiCad negated-angle convention)
    rotated_x, rotated_y = rotate_pad_offset(px, py, fp.rotation)
    abs_x = fx + rotated_x
    abs_y = fy + rotated_y

    return (abs_x, abs_y)
