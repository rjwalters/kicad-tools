"""Zone fill DRC rules.

This module checks that copper zones have been filled (i.e., they contain
actual copper geometry). Zones defined in the PCB but never filled by
KiCad's zone filler will break power/ground connectivity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Zone


def _zone_bounding_box(zone: Zone) -> str:
    """Return a human-readable bounding box string for the zone polygon."""
    if not zone.polygon:
        return "no boundary"
    xs = [p[0] for p in zone.polygon]
    ys = [p[1] for p in zone.polygon]
    return f"({min(xs):.2f}, {min(ys):.2f}) to ({max(xs):.2f}, {max(ys):.2f}) mm"


def _zone_center(zone: Zone) -> tuple[float, float] | None:
    """Return the centroid of the zone boundary polygon."""
    if not zone.polygon:
        return None
    xs = [p[0] for p in zone.polygon]
    ys = [p[1] for p in zone.polygon]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


class ZoneFillRule(DRCRule):
    """Check that copper zones have been filled.

    Detects two conditions:

    1. **Unfilled zone** (``zone_unfilled``): A zone with ``is_filled=True``
       (the designer intends copper fill) but zero ``filled_polygons``
       (KiCad's zone filler was never run, so no actual copper exists).

    2. **Fill disabled** (``zone_fill_disabled``): A zone with
       ``is_filled=False``, meaning the fill flag is explicitly off.

    Both are reported as warnings because they indicate a design intent
    mismatch rather than a hard manufacturing error.

    Keepout zones (rule areas) are already excluded by the PCB parser --
    only ``(zone ...)`` S-expression nodes are parsed into ``pcb.zones``.

    Zones with ``net_number=0`` and an empty ``net_name`` are flagged as
    having an unassigned net, which indicates an incomplete zone definition.
    """

    rule_id = "zone_fill"
    name = "Zone Fill"
    description = "Check that copper zones contain filled polygon data"

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all zones for fill status.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile
                (not used by this rule but required by the interface)

        Returns:
            DRCResults containing zone fill violations
        """
        results = DRCResults()
        results.rules_checked = 1

        for zone in pcb.zones:
            net_label = zone.net_name if zone.net_name else "unassigned"
            layer = zone.layer or "unknown"
            bbox = _zone_bounding_box(zone)
            center = _zone_center(zone)

            if not zone.is_filled:
                # Fill flag is explicitly off
                results.add(
                    DRCViolation(
                        rule_id="zone_fill_disabled",
                        severity="warning",
                        message=(f"Zone fill disabled for net '{net_label}' on {layer} [{bbox}]"),
                        location=center,
                        layer=layer,
                        items=(f"net:{net_label}",),
                    )
                )
            elif len(zone.filled_polygons) == 0:
                # Fill intended but no copper geometry present
                results.add(
                    DRCViolation(
                        rule_id="zone_unfilled",
                        severity="warning",
                        message=(
                            f"Zone for net '{net_label}' on {layer} "
                            f"has fill enabled but no filled polygons [{bbox}]"
                        ),
                        location=center,
                        layer=layer,
                        items=(f"net:{net_label}",),
                    )
                )

            # Additionally flag zones with no net assignment
            if zone.net_number == 0 and not zone.net_name:
                results.add(
                    DRCViolation(
                        rule_id="zone_no_net",
                        severity="warning",
                        message=(f"Zone on {layer} has no net assigned [{bbox}]"),
                        location=center,
                        layer=layer,
                    )
                )

        return results
