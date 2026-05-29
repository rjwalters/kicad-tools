"""Thermal FOM soft term.

Issue #3186 term 9 (thermal spread):
``sum_(power-dissipating part) (power_W * distance_to_copper_pour)``

This term only fires when component-level power metadata is available.
When no parts declare power, the term returns 0 (no-op) so the FOM
doesn't blanket-penalise every board.

Power metadata sources (checked in order):
1. ``footprint.properties["Power_W"]`` -- a per-instance override.
2. ``classify_thermal_properties()`` from
   :mod:`kicad_tools.optim.thermal` -- uses a library of part-family
   defaults (e.g. LDOs, voltage regulators have nonzero TDPs).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from kicad_tools.optim.fom_features import BoardFeatures, euclidean

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = ["thermal_spread"]


def _power_for_footprint(fp_props: dict[str, str]) -> float | None:
    """Return declared power in watts, or None if not declared.

    Recognised property keys (case-insensitive): ``Power_W``, ``power_w``,
    ``Power``, ``TDP_W``.
    """
    for key in ("Power_W", "power_w", "Power", "TDP_W", "tdp_w"):
        if key in fp_props:
            try:
                return float(fp_props[key])
            except (TypeError, ValueError):
                continue
    return None


def thermal_spread(features: BoardFeatures, pcb: PCB) -> float:
    """Power-weighted distance from each dissipating part to nearest copper pour.

    Normalisation
    -------------
    Each dissipating part contributes ``power_W * distance_mm`` to the
    sum, where ``distance_mm`` is the Euclidean distance from the part's
    centre to the centroid of the nearest copper pour (zone).

    0 = every power-dissipating part sits on a copper pour (perfect heat
    spreading); larger = power dissipators isolated from copper.

    When no parts declare power (no metadata, no library hints), the
    term returns 0 without flagging anything as wrong -- thermal is a
    no-op for digital-logic boards.

    The "distance to pour" simplification:
        We use the *centroid* of each zone, not its boundary.  This is
        coarser than measuring perpendicular distance to the zone edge,
        but it is monotonic in the same direction (parts near the pour
        score less) and is dramatically cheaper.  Phase 2 follow-up
        could refine this.
    """
    # Collect zone centroids.
    zone_centroids: list[tuple[float, float]] = []
    for zone in pcb.zones:
        try:
            poly = zone.polygon
        except AttributeError:
            poly = None
        if not poly:
            continue
        # poly is a list of (x, y) points.
        xs = [pt[0] for pt in poly]
        ys = [pt[1] for pt in poly]
        if xs and ys:
            zone_centroids.append((sum(xs) / len(xs), sum(ys) / len(ys)))

    if not zone_centroids:
        return 0.0

    # Try to use the part-family thermal classifier if available.
    fp_props_by_ref: dict[str, dict[str, str]] = {}
    for fp in pcb.footprints:
        if fp.properties:
            fp_props_by_ref[fp.reference] = dict(fp.properties)

    library_powers: dict[str, float] = {}
    try:
        from kicad_tools.optim.thermal import classify_thermal_properties

        thermal_props = classify_thermal_properties(pcb)
        for ref, props in (thermal_props or {}).items():
            tdp = getattr(props, "power_dissipation_w", None)
            if tdp is not None and tdp > 0:
                library_powers[ref] = float(tdp)
    except Exception:
        # No library info -- only per-instance metadata will fire.
        pass

    total = 0.0
    for fp_feature in features.footprints:
        power = _power_for_footprint(fp_props_by_ref.get(fp_feature.reference, {}))
        if power is None:
            power = library_powers.get(fp_feature.reference)
        if power is None or power <= 0:
            continue

        # Nearest zone centroid.
        min_dist = math.inf
        for zc in zone_centroids:
            d = euclidean((fp_feature.x, fp_feature.y), zc)
            if d < min_dist:
                min_dist = d
        if math.isfinite(min_dist):
            total += power * min_dist
    return total
