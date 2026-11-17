"""Solder-mask opening warnings from the JLCDFM mask checks (Issue #8847).

Two default-on, pure-geometry rules built on
:func:`kicad_tools.validate.mask_geometry.inspect_mask_geometry`:

* ``mask_negative_expansion`` -- a pad's effective mask margin is ``< 0``, so
  the opening is smaller than its copper.  Zero is not flagged; a per-pad
  override already wins over the footprint/board value in the snapshot.
* ``mask_multi_segment`` -- one opening whose geometry is more than one
  disjoint polygon, which the fab reads as several pieces.

Both are warnings with no numeric threshold (the limit is 0 mm).  They do not
use the opt-in native ``mask_to_copper`` path.
"""

from __future__ import annotations

from typing import Any

from ..violations import DRCResults, DRCViolation

NEGATIVE_RULE_ID = "mask_negative_expansion"
MULTI_RULE_ID = "mask_multi_segment"
_EPS = 1e-9


def _pad_index(pcb: Any) -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for fp in getattr(pcb, "footprints", None) or []:
        for pad in fp.pads:
            if getattr(pad, "uuid", ""):
                index[pad.uuid] = (fp.reference, str(pad.number))
    return index


def check_mask_openings(snapshot: Any, pcb: Any, which: str) -> DRCResults:
    """``which`` is ``"negative"`` or ``"multi"``."""
    results = DRCResults()
    rule_id = NEGATIVE_RULE_ID if which == "negative" else MULTI_RULE_ID
    results.rules_checked += 1
    results.rules_checked_by_rule[rule_id] = 1
    pads = _pad_index(pcb)
    for opening in snapshot.openings:
        geom = opening.geometry
        if which == "negative":
            if not opening.margin_mm < -_EPS:
                continue
            msg = (
                f"mask opening is {-opening.margin_mm:.3f}mm smaller than its pad "
                f"(negative expansion {opening.margin_mm:.3f}mm, from {opening.margin_source})"
            )
            actual = round(opening.margin_mm, 4)
        else:
            parts = len(getattr(geom, "geoms", [])) if geom.geom_type.startswith("Multi") else 1
            if parts <= 1:
                continue
            msg = f"mask opening splits into {parts} disjoint pieces"
            actual = float(parts)
        ref, num = pads.get(opening.source_uuid, ("", ""))
        label = f"{ref}.{num}" if ref else f"{opening.kind} {opening.source_uuid[:8]}"
        if geom.is_empty:
            loc_geom = opening.copper
        else:
            loc_geom = geom
        c = loc_geom.centroid
        results.add(
            DRCViolation(
                rule_id=rule_id,
                severity="warning",
                message=f"{label} ({opening.layer}): {msg}",
                location=(round(c.x, 3), round(c.y, 3)),
                layer=opening.layer,
                actual_value=actual,
                items=(ref,) if ref else (),
            )
        )
    return results
