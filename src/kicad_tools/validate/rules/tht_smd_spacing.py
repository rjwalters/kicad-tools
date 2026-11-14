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

**Vias are judged by drill diameter, not by whether KiCad calls them a pad**
(Issue #8914). The fab classes a hole by its size: on the board behind #8914,
JLCDFM reported three top-side *danger* items that kct missed entirely, and all
three were wire-anchor **vias** with 1.3-1.42 mm drills against SMD pads --
while the footprint THT pad at the same distance *was* reported. A hole big
enough to take a wire or a component lead is a wave/selective-solder and
hand-rework feature regardless of which s-expression node holds it, so a via
whose drill is at least :data:`ANCHOR_DRILL_MM` is judged exactly like a THT
component pad. A routine fan-out via is not: judging *every* via restores the
flood the old ``include_vias=False`` default existed to prevent. Measured
2026-11-14 on this repo's routed boards, ``via_drill_mm=0`` (judge every via)
against the 1.0 mm default:

* board 03: 461 findings vs 6     * board 05: 610 vs 0
* board 06: 176 findings vs 0     * board 07: 1,068 vs 0

Every via on every one of those boards drills 0.15-0.3 mm, so the default leaves
each of them judged exactly as the vias-excluded default did -- the fix adds the
fab's anchor-hole class without adding a single finding to a board that has
none.

A pad whose ``shape`` this rule cannot measure is **reported as unmeasured**
(``tht_smd_spacing_incomplete``, info) rather than silently dropped: a shape the
rule cannot judge must not look like a pad it cleared (Issue #8914).
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

#: Drill diameter (mm) at or above which a via is judged like a THT component
#: pad.  1.0 mm is just under 18 AWG solid wire (1.02 mm): a hole that takes a
#: wire or a lead is a solder/rework feature, a fan-out via (0.2-0.5 mm on every
#: board in this repo) is not.  Calibrated against the #8914 report, whose
#: missed anchor vias drill 1.3-1.42 mm.
ANCHOR_DRILL_MM = 1.0

#: Pad shapes whose true copper outline :func:`_pad_polygon` reproduces.  Any
#: other shape (``custom``, ``trapezoid``) would be approximated by its
#: bounding rectangle, so it is reported unmeasured instead of mismeasured.
MEASURABLE_SHAPES = frozenset({"circle", "rect", "oval", "obround", "roundrect"})

#: Cap on the pad identities listed in one ``tht_smd_spacing_incomplete``
#: message, so a board full of custom pads yields a long message rather than a
#: flood of findings.
_MAX_LISTED_IDENTITIES = 10


def check_tht_smd_spacing(
    pcb,
    danger_mm: float = DANGER_MM,
    warning_mm: float = WARNING_MM,
    *,
    via_drill_mm: float = ANCHOR_DRILL_MM,
) -> DRCResults:
    """Report THT pads and anchor-sized vias closer than the thresholds to any SMD pad.

    ``via_drill_mm`` is the drill diameter at which a via starts being judged
    like a THT component pad (:data:`ANCHOR_DRILL_MM` by default; see the module
    docstring for the measured reason it is not zero).  ``0`` judges every via
    and ``math.inf`` judges none; THT *component* pads are judged at every
    drill size either way.
    """
    from shapely import STRtree  # type: ignore[import-untyped]
    from shapely.geometry import Point  # type: ignore[import-untyped]
    from shapely.ops import nearest_points  # type: ignore[import-untyped]

    if not (math.isfinite(danger_mm) and math.isfinite(warning_mm)) or danger_mm <= 0:
        raise ValueError("THT/SMD spacing thresholds must be finite and positive")
    if math.isnan(via_drill_mm) or via_drill_mm < 0:
        raise ValueError("THT/SMD via drill threshold must be a non-negative number")
    results = DRCResults(rules_checked=1)
    layers = [layer.name for layer in pcb.copper_layers]
    origin = board_origin(pcb)
    ox, oy = getattr(pcb, "board_origin", (0.0, 0.0))

    # (layer, identity, geometry, net, footprint) / (layers, identity, geometry, net, footprint)
    smd: list[tuple[str, str, Any, str, int | None]] = []
    tht: list[tuple[set[str], str, Any, str, int | None]] = []
    # shape -> identities of pads this rule cannot measure (Issue #8914).
    unmeasured: dict[str, list[str]] = {}
    for fp in pcb.footprints:
        for index, pad in enumerate(fp.pads):
            if pad.type not in {"smd", "thru_hole"}:
                continue
            identity = pad.uuid or f"{fp.reference}:{pad.number}:pad:{index}"
            if pad.shape not in MEASURABLE_SHAPES:
                unmeasured.setdefault(pad.shape, []).append(identity)
                continue
            geom = _pad_polygon(pad, fp)
            if geom is None or geom.is_empty:
                continue
            net = str(pad.net_number)
            owner = id(fp)
            if pad.type == "smd":
                for layer in layers:
                    if _pad_on_layer(pad, layer):
                        smd.append((layer, identity, geom, net, owner))
            else:
                tht.append((set(layers), identity, geom, net, owner))
    for shape in sorted(unmeasured):
        identities = sorted(unmeasured[shape])
        listed = ", ".join(identities[:_MAX_LISTED_IDENTITIES])
        if len(identities) > _MAX_LISTED_IDENTITIES:
            listed += f", +{len(identities) - _MAX_LISTED_IDENTITIES} more"
        results.add(
            DRCViolation(
                "tht_smd_spacing_incomplete",
                # Reported (never a silent pass) at info: an unmeasurable pad
                # shape is missing coverage, not a defect of the board, so it
                # must not block a board whose other pads are all clean.
                "info",
                f"THT/SMD spacing coverage incomplete: {len(identities)} pad(s) of "
                f"shape {shape!r} were not measured (this rule reproduces only "
                f"{', '.join(sorted(MEASURABLE_SHAPES))}); spacing around them is "
                f"unjudged, not cleared: {listed}",
            )
        )
    for via in pcb.vias:
        # ``not (drill >= limit)`` rather than ``drill < limit`` so a NaN drill
        # is skipped instead of silently judged, and so ``math.inf`` disables
        # via judging outright.
        if not via.drill >= via_drill_mm - TOLERANCE:
            continue
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
