"""THT / via to SMD-pad spacing rule (Issue #8843, epic #8838).

JLCDFM flags through-hole pads and vias that sit close to an SMD pad (wave /
selective-solder and rework clearance). Observed thresholds (epic #8838,
2026-11-12 -- observed from JLCDFM output, not documented by the fab):

* gap < :data:`DANGER_MM` (2.0 mm)  -> ``error``   (JLCDFM "danger")
* gap < :data:`WARNING_MM` (2.7 mm) -> ``warning``

Measurement reference (a documented choice -- the epic does not say whether
JLCDFM measures hole centre, hole edge or copper edge): **copper edge to copper
edge**, i.e. the shortest distance between the THT pad's (or via annulus's)
copper outline and the SMD pad's copper outline. Pads/vias with a gap of ~0
(a via inside an SMD pad, overlapping copper) are not reported here; that is
``via_in_pad`` territory.

"Same side": a THT barrel has copper on every layer it spans, so it is compared
against SMD pads on any layer the barrel reaches; a pad on F.Cu is never
compared with a via that only spans inner layers. Any net, any footprint.
Non-plated (``np_thru_hole``) holes are ignored.

Pads of the *same footprint* are never compared with each other: a connector's
shield / mounting tabs and its own SMD pins are placed by the library
footprint, not by the board designer, so a short gap there is not a placement
defect (board 06's J1/J4 produced all of its findings this way).
"""

from __future__ import annotations

import math
from typing import Any

from ..copper_refs import board_origin, via_ref
from ..violations import DRCResults, DRCViolation
from .clearance import _pad_on_layer, _pad_polygon

TOLERANCE = 0.001
DANGER_MM = 2.0
WARNING_MM = 2.7


def check_tht_smd_spacing(
    pcb,
    danger_mm: float = DANGER_MM,
    warning_mm: float = WARNING_MM,
    *,
    include_vias: bool = False,
) -> DRCResults:
    """Report THT pads (and, opt-in, vias) closer than the thresholds to any SMD pad.

    ``include_vias`` is off by default: vias are routinely fanned out within
    2 mm of SMD pads on every routed board (board 06 alone yields ~1,480 via
    findings against 18 THT-pad findings), and a via barrel is not a
    wave/selective-solder feature. The default therefore judges plated
    through-hole *component* pads only.
    """
    from shapely import STRtree  # type: ignore[import-untyped]
    from shapely.geometry import Point  # type: ignore[import-untyped]
    from shapely.ops import nearest_points  # type: ignore[import-untyped]

    if not (math.isfinite(danger_mm) and math.isfinite(warning_mm)) or danger_mm <= 0:
        raise ValueError("THT/SMD spacing thresholds must be finite and positive")
    results = DRCResults(rules_checked=1)
    layers = [layer.name for layer in pcb.copper_layers]
    origin = board_origin(pcb)
    ox, oy = getattr(pcb, "board_origin", (0.0, 0.0))

    # (layer, identity, geometry, net, footprint) / (layers, identity, geometry, net, footprint)
    smd: list[tuple[str, str, Any, str, int | None]] = []
    tht: list[tuple[set[str], str, Any, str, int | None]] = []
    for fp in pcb.footprints:
        for index, pad in enumerate(fp.pads):
            if pad.type not in {"smd", "thru_hole"}:
                continue
            if pad.shape not in {"circle", "rect", "oval", "obround", "roundrect"}:
                continue
            geom = _pad_polygon(pad, fp)
            if geom is None or geom.is_empty:
                continue
            identity = pad.uuid or f"{fp.reference}:{pad.number}:pad:{index}"
            net = str(pad.net_number)
            owner = id(fp)
            if pad.type == "smd":
                for layer in layers:
                    if _pad_on_layer(pad, layer):
                        smd.append((layer, identity, geom, net, owner))
            else:
                tht.append((set(layers), identity, geom, net, owner))
    for via in pcb.vias if include_vias else ():
        if any(layer not in layers for layer in via.layers):
            continue
        first, last = sorted(layers.index(layer) for layer in via.layers)
        tht.append(
            (
                set(layers[first : last + 1]),
                via_ref(via, origin),
                Point(via.position).buffer(via.size / 2, quad_segs=64),
                str(via.net_number),
                None,
            )
        )

    for layer in sorted({s[0] for s in smd}):
        pads = [s for s in smd if s[0] == layer]
        tree = STRtree([p[2] for p in pads])
        for span, identity, geom, net, tht_owner in tht:
            if layer not in span:
                continue
            for raw in tree.query(geom, predicate="dwithin", distance=warning_mm):
                _, smd_id, smd_geom, smd_net, smd_owner = pads[int(raw)]
                if smd_id == identity or (tht_owner is not None and tht_owner == smd_owner):
                    continue
                distance = geom.distance(smd_geom)
                if distance <= TOLERANCE or distance >= warning_mm - TOLERANCE:
                    continue
                a, b = nearest_points(geom, smd_geom)
                is_error = distance < danger_mm - TOLERANCE
                results.add(
                    DRCViolation(
                        "tht_smd_spacing",
                        "error" if is_error else "warning",
                        f"THT/via to SMD pad spacing {distance:.3f}mm < "
                        f"{(danger_mm if is_error else warning_mm):.1f}mm "
                        f"(copper edge to copper edge) on {layer}; closest points "
                        f"({a.x + ox:.6f}, {a.y + oy:.6f}) and ({b.x + ox:.6f}, {b.y + oy:.6f})",
                        location=((a.x + b.x) / 2, (a.y + b.y) / 2),
                        layer=layer,
                        actual_value=distance,
                        required_value=danger_mm if is_error else warning_mm,
                        items=tuple(sorted((identity, smd_id))),
                        nets=tuple(sorted({net, smd_net})),
                    )
                )
    return results
