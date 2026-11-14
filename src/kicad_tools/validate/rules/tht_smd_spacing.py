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

**An exposed via annulus also counts as the "SMD" side** (Issue #8925).
JLCDFM's fifth #8914 danger item paired a 1.9 mm / 1.3 mm-drill anchor via
with an ordinary 0.8 mm / 0.4 mm fan-out via 1.5 mm away (same net): the fab's
"SMD" side includes a small exposed via pad, not only footprint SMD pads. Two
gates keep this from becoming the via flood above:

* **Hole side: anchor vias only.** Only a *via* already judged as a hole (drill
  at least ``via_drill_mm``, :data:`ANCHOR_DRILL_MM` by default) is paired
  against via annuli. A THT *component* pad is never paired against a via
  annulus: nothing in the fab report shows that pairing, and allowing it added
  20 errors + 4 warnings to board 05 (measured 2026-11-14, #8925 curation).
  Fan-out-vs-fan-out pairs are never judged.
* **Annulus side: exposed copper only (tenting decision).** The fab's hazard is
  exposed copper taking solder, so a via annulus is a candidate on ``F.Cu`` /
  ``B.Cu`` only where it is *not tented* on that side, resolved by
  :func:`~kicad_tools.validate.rules.silkscreen._via_is_tented` (per-via
  override -> board setup default -> absent-token default: tented). Inner
  layers carry no mask and take no solder, so an annulus there is never a
  candidate. The hole side is not exposure-gated: a plated barrel is a hole
  regardless of what covers its ring. The reporter noted the anchor via of
  their item 1 was untented on both sides, which suggests tenting is part of
  the fab's test, but **the fab's exact test is unverified**.

No board in this repo's regression set has an anchor-sized via, and every via
on boards 03 / 05 / 06 / 07 resolves as tented, so this pairing adds nothing to
their counts (6 / 0 / 0 / 0); its live calibration is unverified. A pair is
reported **once**, even when it meets on both faces or both vias are anchors
(each then sits on both sides of the comparison); the message names every
layer it meets on.

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
from .silkscreen import _via_is_tented

TOLERANCE = 0.001
DANGER_MM = 2.0
WARNING_MM = 2.7

#: Drill diameter (mm) at or above which a via is judged like a THT component
#: pad.  1.0 mm is just under 18 AWG solid wire (1.02 mm): a hole that takes a
#: wire or a lead is a solder/rework feature, a fan-out via (0.2-0.5 mm on every
#: board in this repo) is not.  Calibrated against the #8914 report, whose
#: missed anchor vias drill 1.3-1.42 mm.
ANCHOR_DRILL_MM = 1.0

#: Outer copper layers and the solder-mask side :func:`_via_is_tented` resolves
#: for each.  An inner-layer annulus has no mask opening and takes no solder.
_MASK_SIDE = {"F.Cu": "F", "B.Cu": "B"}

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

    Anchor-sized vias are also judged against every *exposed* via annulus
    (Issue #8925); THT component pads are not.

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
    # Exposed via annuli: the fab's other "SMD" side (Issue #8925), judged only
    # against anchor vias -- never against THT component pads.
    # (layer, identity, geometry, net)
    annuli: list[tuple[str, str, Any, str]] = []
    # Indices into ``tht`` of the entries that are anchor vias.
    anchor_vias: list[int] = []
    setup = getattr(pcb, "setup", None)
    for via in pcb.vias:
        if any(layer not in layers for layer in via.layers):
            continue
        first, last = sorted(layers.index(layer) for layer in via.layers)
        span = set(layers[first : last + 1])
        identity = via_ref(via, origin)
        geom = Point(via.position).buffer(via.size / 2, quad_segs=64)
        net = str(via.net_number)
        for layer in sorted(span):
            side = _MASK_SIDE.get(layer)
            if side is not None and not _via_is_tented(via, setup, side):
                annuli.append((layer, identity, geom, net))
        # ``not (drill >= limit)`` rather than ``drill < limit`` so a NaN drill
        # is skipped instead of silently judged, and so ``math.inf`` disables
        # via judging outright.
        if not via.drill >= via_drill_mm - TOLERANCE:
            continue
        anchor_vias.append(len(tht))
        tht.append((span, identity, geom, net, None))

    def report(layer, identity, geom, net, other_id, other_geom, other_net, what, where=None):
        distance = geom.distance(other_geom)
        if distance <= TOLERANCE or distance >= warning_mm - TOLERANCE:
            return
        a, b = nearest_points(geom, other_geom)
        is_error = distance < danger_mm - TOLERANCE
        results.add(
            DRCViolation(
                "tht_smd_spacing",
                "error" if is_error else "warning",
                f"THT/via to {what} spacing {distance:.3f}mm < "
                f"{(danger_mm if is_error else warning_mm):.1f}mm "
                f"(copper edge to copper edge) on {where or layer}; closest points "
                f"({a.x + ox:.6f}, {a.y + oy:.6f}) and ({b.x + ox:.6f}, {b.y + oy:.6f})",
                location=((a.x + b.x) / 2, (a.y + b.y) / 2),
                layer=layer,
                actual_value=distance,
                required_value=danger_mm if is_error else warning_mm,
                items=tuple(sorted((identity, other_id))),
                nets=tuple(sorted({net, other_net})),
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
                report(layer, identity, geom, net, smd_id, smd_geom, smd_net, "SMD pad")

    # An anchor-vs-annulus pair is one physical feature pair: two anchor vias
    # are each on both sides of this comparison, and a pair exposed on both
    # faces meets on two layers.  Report it once (``DRCResults.add`` does not
    # de-duplicate), on the first layer, naming every layer it meets on.
    # pair -> (layers, hole entry, ring entry)
    pairs: dict[tuple[str, str], tuple[list[str], tuple[str, Any, str], tuple[str, Any, str]]] = {}
    for layer in sorted({a[0] for a in annuli}):
        rings = [a for a in annuli if a[0] == layer]
        tree = STRtree([r[2] for r in rings])
        for index in anchor_vias:
            span, identity, geom, net, _ = tht[index]
            if layer not in span:
                continue
            for raw in tree.query(geom, predicate="dwithin", distance=warning_mm):
                _, ring_id, ring_geom, ring_net = rings[int(raw)]
                if ring_id == identity:
                    continue
                key = (min(identity, ring_id), max(identity, ring_id))
                entry = pairs.setdefault(
                    key, ([], (identity, geom, net), (ring_id, ring_geom, ring_net))
                )
                if layer not in entry[0]:
                    entry[0].append(layer)
    for key in sorted(pairs):
        pair_layers, hole, ring = pairs[key]
        hole_id, hole_geom, hole_net = hole
        ring_id, ring_geom, ring_net = ring
        report(
            pair_layers[0],
            hole_id,
            hole_geom,
            hole_net,
            ring_id,
            ring_geom,
            ring_net,
            "exposed via annulus",
            ", ".join(pair_layers),
        )
    return results
