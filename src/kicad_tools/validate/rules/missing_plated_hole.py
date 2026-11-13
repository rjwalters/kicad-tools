"""Missing plated through-hole DRC rule (Issue #8840).

JLCDFM warns when a top-layer pad and a bottom-layer pad sit on the same
centre with no hole joining them: the designer meant a through-hole
connection but drew two SMD pads, so the layers are not connected.

Rule ID generated:
    - missing_plated_hole: an F.Cu-only SMD pad and a B.Cu-only SMD pad of
      one net have centres within :data:`COLOCATION_TOLERANCE_MM`, and no
      same-net plated hole (via or plated through-hole pad) lies inside both
      pads.  A thermal pad whose top and bottom copper are joined by a via
      array is therefore clean even with no hole at its centre; a
      non-plated hole never counts, because it does not join the layers.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

MISSING_PLATED_HOLE_RULE_ID = "missing_plated_hole"

#: Centres closer than this (mm) count as co-located.
COLOCATION_TOLERANCE_MM = 0.05


def _pad_centres(pcb: PCB):
    from kicad_tools.core.geometry import rotate_pad_offset

    for fp in pcb.footprints:
        for pad in fp.pads:
            dx, dy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            yield fp, pad, (fp.position[0] + dx, fp.position[1] + dy)


def _inside_pad(pad, centre: tuple[float, float], point: tuple[float, float]) -> bool:
    """Whether ``point`` lies within the pad's bounding rectangle (pad frame)."""
    from kicad_tools.core.geometry import rotate_pad_offset

    # ``pad.rotation`` is absolute; undo it to reach the pad's own frame.
    lx, ly = rotate_pad_offset(point[0] - centre[0], point[1] - centre[1], -pad.rotation)
    tol = 1e-6
    return abs(lx) <= pad.size[0] / 2 + tol and abs(ly) <= pad.size[1] / 2 + tol


class MissingPlatedHoleRule(DRCRule):
    """Flag co-located top/bottom SMD pads that have no hole between them."""

    rule_id = MISSING_PLATED_HOLE_RULE_ID
    name = "Missing Plated Hole"
    description = "Flags co-located top and bottom SMD pads without a plated hole"

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Return one ``missing_plated_hole`` warning per co-located pad pair."""
        del design_rules
        results = DRCResults()
        results.rules_checked = 1
        top, bottom = [], []
        # Plated holes as (net_number, centre): only these join the layers.
        holes: list[tuple[int, tuple[float, float]]] = []
        for fp, pad, centre in _pad_centres(pcb):
            if pad.type == "np_thru_hole":
                continue
            if pad.type == "thru_hole" or pad.drill > 0:
                holes.append((pad.net_number, centre))
            elif pad.type == "smd":
                if "F.Cu" in pad.layers and "B.Cu" not in pad.layers:
                    top.append((fp, pad, centre))
                elif "B.Cu" in pad.layers and "F.Cu" not in pad.layers:
                    bottom.append((fp, pad, centre))
        holes.extend((v.net_number, (v.position[0], v.position[1])) for v in pcb.vias)

        for fp_t, pad_t, c_t in top:
            for fp_b, pad_b, c_b in bottom:
                if math.dist(c_t, c_b) > COLOCATION_TOLERANCE_MM:
                    continue
                # Different nets (e.g. card-edge fingers A1/B1): joining them
                # with a hole would short the nets, so it is not a missing hole.
                if pad_t.net_number != pad_b.net_number:
                    continue
                if any(
                    net == pad_t.net_number
                    and _inside_pad(pad_t, c_t, h)
                    and _inside_pad(pad_b, c_b, h)
                    for net, h in holes
                ):
                    continue
                results.add(
                    DRCViolation(
                        rule_id=MISSING_PLATED_HOLE_RULE_ID,
                        severity="warning",
                        message=(
                            f"Top pad {fp_t.reference}.{pad_t.number} and bottom pad "
                            f"{fp_b.reference}.{pad_b.number} are co-located with no "
                            f"plated hole between them"
                        ),
                        location=c_t,
                        layer="F.Cu",
                        items=(
                            f"{fp_t.reference}.{pad_t.number}",
                            f"{fp_b.reference}.{pad_b.number}",
                        ),
                    )
                )
        return results
