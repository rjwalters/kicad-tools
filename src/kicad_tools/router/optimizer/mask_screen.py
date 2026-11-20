"""Conservative mask-to-copper screen for the ``--drc-aware`` referee (issue #9748).

``kct check --mfr`` evaluates ``mask_to_copper`` through the native KiCad
object export (:func:`kicad_tools.validate.mask_copper.check_mask_to_copper`),
which needs a qualified native interpreter and staged source files -- far too
heavy to launch once per referee trial inside ``optimize-traces``.  Before
this module the referee therefore counted every DRC category *except*
``mask_to_copper``, and its ``DRC errors: 0 -> 0 (no regressions)`` summary
stayed silent while a rewrite turned mask-clean pad escapes into
``mask_to_copper`` errors.

This screen is the referee's conservative stand-in, not a re-implementation
of the reporter: it reuses the pure-Python
:func:`kicad_tools.validate.mask_geometry.inspect_mask_geometry` snapshot
(exact standard-pad/via openings, source-bound) and counts ``(opening,
conductor)`` pairs whose disposition the native rules in
:func:`kicad_tools.validate.mask_copper.assess_attributed_geometry` /
``_within_own_pad`` would read as error class -- copper overlapping an
opening, or within the policy clearance of it, minus the one-clean-entry
allowance.  The count never reaches the user as a mask finding; it keys the
referee's ``mask_to_copper`` category so per-net growth forces the rollback
gate, and the summary line reports whether the screen ran.

Deliberately conservative in both directions, and documented as such:

* openings the pure snapshot cannot model (custom/chamfered/padstack pads,
  mask graphics, merged native regions, project ``.kicad_dru`` rules) make
  the snapshot *incomplete*; the screen then refuses to count at all
  (``not_evaluated``) rather than under-report.
* conductors are tracks and vias only.  Pads and zone fills never move in
  this optimizer, so their (pre-existing) exposures cancel between the
  baseline and every trial board.
* reinforce-run sidecars (``<board>.reinforcement.json``) are not staged
  beside the scratch board, so copper a run intentionally exposes can count
  here.  Both sides of every comparison carry the same over-count, so only
  a rewrite that moves copper near such an opening can be (conservatively)
  rolled back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Parity with the native measurement rules: a straight entry crossing the
# opening edge at 45 degrees or steeper leaves at most sqrt(2) x width x
# clearance of copper in the band beside the opening.  A test fails if these
# drift from ``kicad_tools.validate.mask_copper``.
_ENTRY_CROSSING_FACTOR = 1.45
_AREA_EPSILON_MM2 = 1e-9

#: The pure snapshot is analytic (no Gerber reconstruction), so its only
#: uncertainty is the polygonal approximation of circles -- far below the
#: native plot budget.  A small numeric slack only.
_SCREEN_UNCERTAINTY_MM = 0.0001


class MaskExposureScreen:
    """Counts error-class mask-exposure pairs for one manufacturer policy.

    Build via :func:`mask_exposure_screen_for_manufacturer`.  ``status`` is
    ``"screened"`` once a count has run on a complete snapshot, or
    ``"not_evaluated"`` when the snapshot could not model the board -- and
    then the screen latches off, so the baseline and every trial count the
    same categories and the rollback gate never compares mixed totals.
    """

    def __init__(self, clearance_mm: float) -> None:
        self.clearance_mm = clearance_mm
        self.status = "screened"
        self._latched_off = False

    def count(self, board_path: str | Path, pcb: Any) -> int | None:
        """Error-class ``(opening, conductor)`` pairs on *board_path*.

        ``pcb`` is the schema :class:`~kicad_tools.schema.pcb.PCB` already
        loaded from the same path (the referee's own object; the snapshot's
        board-relative coordinate space).  ``None`` means the screen could
        not evaluate this board (incomplete snapshot) and no ``mask_to_copper``
        count may be claimed for it.
        """
        if self._latched_off:
            return None
        from kicad_tools.validate.mask_geometry import inspect_mask_geometry

        try:
            snapshot = inspect_mask_geometry(board_path)
        except Exception:
            snapshot = None
        if snapshot is None or not snapshot.complete:
            self.status = "not_evaluated"
            self._latched_off = True
            return None
        self.status = "screened"
        return self._count_pairs(snapshot, pcb)

    def _count_pairs(self, snapshot: Any, pcb: Any) -> int:
        from shapely import STRtree  # type: ignore[import-untyped]
        from shapely.geometry import Point, box  # type: ignore[import-untyped]

        clearance = self.clearance_mm
        u = _SCREEN_UNCERTAINTY_MM

        pad_net: dict[str, str] = {}
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.uuid:
                    pad_net[pad.uuid] = pad.net_name or ""

        # Conductor copper, buffered once: rounded capsules for tracks,
        # circles for vias (the shapes the native export attributes).
        by_layer: dict[str, list[dict[str, Any]]] = {"F.Cu": [], "B.Cu": []}
        for seg in pcb.segments:
            if seg.layer not in by_layer or seg.width <= 0:
                continue
            # No ignore: the shapely.geometry stubs error is consumed at
            # the first import site above; a second would read as unused.
            from shapely.geometry import LineString

            by_layer[seg.layer].append(
                {
                    "kind": "segment",
                    "net": seg.net_name or "",
                    "uuid": seg.uuid or "",
                    "shape": LineString([seg.start, seg.end]).buffer(seg.width / 2.0),
                    "endpoints": (seg.start, seg.end),
                    "width": seg.width,
                    "drill": 0.0,
                }
            )
        for via in pcb.vias:
            if via.size <= 0:
                continue
            for layer in ("F.Cu", "B.Cu"):
                if layer not in via.layers:
                    continue
                by_layer[layer].append(
                    {
                        "kind": "via",
                        "net": via.net_name or "",
                        "uuid": via.uuid or "",
                        "shape": Point(*via.position).buffer(via.size / 2.0),
                        "endpoints": (via.position, via.position),
                        "width": via.size,
                        "drill": via.drill,
                    }
                )
        trees = {
            layer: STRtree([c["shape"] for c in conductors]) if conductors else None
            for layer, conductors in by_layer.items()
        }

        findings = 0
        for opening in snapshot.openings:
            layer = opening.layer.replace(".Mask", ".Cu")
            tree = trees.get(layer)
            geom = opening.geometry
            if tree is None or geom is None or geom.is_empty:
                continue
            shrunk = geom.buffer(-u)
            grown_clearance = geom.buffer(clearance + u)
            grown_slack = geom.buffer(u)
            owner_net = pad_net.get(opening.source_uuid, "")
            owner_copper = opening.copper
            minx, miny, maxx, maxy = geom.bounds
            reach = box(
                minx - clearance - u,
                miny - clearance - u,
                maxx + clearance + u,
                maxy + clearance + u,
            )
            for index in tree.query(reach):
                conductor = by_layer[layer][int(index)]
                if conductor["uuid"] and conductor["uuid"] == opening.source_uuid:
                    continue  # the opening's own via copper
                copper = conductor["shape"]
                distance = geom.distance(copper)
                shrunk_copper = copper.buffer(-u)
                certain_exposure = (
                    not shrunk.is_empty
                    and not shrunk_copper.is_empty
                    and shrunk.intersection(shrunk_copper).area > _AREA_EPSILON_MM2
                )
                if not (certain_exposure or distance < clearance - u):
                    continue  # warning band or clear of the opening
                if (
                    owner_net
                    and conductor["net"] == owner_net
                    and opening.kind == "pad"
                    and opening.margin_mm >= 0
                    and _within_own_pad(
                        geom,
                        owner_copper,
                        shrunk,
                        grown_clearance,
                        grown_slack,
                        conductor,
                        clearance,
                        u,
                    )
                ):
                    continue
                findings += 1
        return findings


def _within_own_pad(
    opening: Any,
    owner_copper: Any,
    shrunk_opening: Any,
    grown_clearance: Any,
    grown_slack: Any,
    conductor: dict[str, Any],
    clearance: float,
    u: float,
) -> bool:
    """Mirror of ``mask_copper._within_own_pad`` conditions 2-5 for the screen.

    Condition 1 (owner kind and non-negative margin) is the caller's gate:
    the pure snapshot only emits standard pad shapes, so a complete snapshot
    satisfies the shape whitelist by construction.  Everything else follows
    the native docstring: direct connection, nothing exposed beyond the pad,
    nothing beside the pad beyond one clean entry crossing, and (vias) the
    drill outside the opening.
    """
    # No ignore: see the first shapely import site in count().
    from shapely.geometry import Point

    copper = conductor["shape"]
    core = owner_copper.buffer(-u)
    shrunk_copper = copper.buffer(-u)
    if core.is_empty or shrunk_copper.is_empty:
        return False
    if core.intersection(shrunk_copper).area <= _AREA_EPSILON_MM2:
        return False  # condition 2: no direct connection
    if opening.intersection(copper).difference(owner_copper.buffer(u)).area > _AREA_EPSILON_MM2:
        return False  # condition 3: copper exposed beyond the owner pad
    band = copper.difference(grown_slack).intersection(grown_clearance).area
    allowance = 0.0
    if conductor["kind"] == "segment":
        if not any(core.contains(Point(p)) for p in conductor["endpoints"]):
            return False  # pass-through, straddle or along-edge copper
        allowance = _ENTRY_CROSSING_FACTOR * conductor["width"] * clearance
        ring = grown_clearance.difference(grown_slack)
        if ring.difference(copper).area <= _AREA_EPSILON_MM2:
            # A bar wider than the pad covers the whole perimeter band; the
            # copper left beside the opening is the pad's own perimeter.
            allowance = ring.area
    if band - allowance > _AREA_EPSILON_MM2:
        return False  # condition 4: beside-pad copper beyond one clean entry
    if conductor["kind"] == "via":
        if opening.distance(copper.centroid) < conductor["drill"] / 2.0 + u:
            return False  # condition 5: drill not wholly outside the opening
    return True


def mask_exposure_screen_for_manufacturer(manufacturer: str | None) -> MaskExposureScreen | None:
    """Screen for *manufacturer*, or ``None`` when no cited mask policy exists.

    Reuses the exact policy ``kct check --mfr`` engages by default
    (:func:`kicad_tools.validate.mask_copper.default_request_for_manufacturer`),
    so the referee's clearance floor and the reported check can never drift.
    """
    from kicad_tools.validate.mask_copper import default_request_for_manufacturer

    request = default_request_for_manufacturer(manufacturer)
    if request is None or request.policy is None:
        return None
    return MaskExposureScreen(request.policy.clearance_mm)
