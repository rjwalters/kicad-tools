"""Wire-run vs component-courtyard DRC rule (Issue #8820).

A wire-reinforced run (#8741) is a bus wire soldered along an exposed trace,
1-2 mm tall on the board surface. Nothing else in ``kct check`` treats it as a
physical object, so a run can pass under a same-side component body and every
gate stays green. This rule re-derives each reinforced leg from board copper
(the same derivation as :class:`ReinforcementIncompleteRule`, never the
sidecar's anchor list) and checks two things:

* **run** -- the leg's envelope (wire radius + :data:`RUN_CLEARANCE_MM`) against
  every courtyard on the *same* side as the run, including the footprint the
  wire terminates at. A footprint with no courtyard on that side falls back to
  its fab-layer bounding box. On the side *opposite* a through-hole part only
  its pins protrude, so there the envelope is checked against foreign-net pads
  (pad radius + :data:`HOLE_FILLET_MM`); own-net pins may be soldered to.
* **end hole** -- each anchor hole's envelope (annulus radius +
  :data:`HOLE_FILLET_MM`) against courtyards on *both* sides of the board: the
  hole is a through hole, so the tail and fillet exit opposite the run. A
  footprint the hole itself terminates in is not an obstacle for that hole.

Findings are always errors; waive through ``.kct_waivers.json`` like any rule.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from ..violations import DRCResults, DRCViolation
from .reinforcement_incomplete import (
    ANCHOR_ON_LEG_SLACK_MM,
    DRILL_TOLERANCE_MM,
    ReinforcementIncompleteRule,
    _Leg,
)

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint

#: Soldering clearance (mm) added to the wire radius around a run.
RUN_CLEARANCE_MM = 1.0

#: Fillet clearance (mm) added to a hole's / pin's annulus radius.
HOLE_FILLET_MM = 0.75

_Point = tuple[float, float]


def _side_of_layer(layer: str) -> str | None:
    if layer == "F.Cu":
        return "F"
    if layer == "B.Cu":
        return "B"
    return None


def _fp_side(fp: Footprint) -> str:
    return "B" if str(fp.layer).startswith("B.") else "F"


def _body_polygon(fp: Footprint, side: str, Polygon: Any, box: Any) -> Any | None:
    """Courtyard of ``fp`` on ``side``; else fab bbox if the part sits on ``side``."""
    from kicad_tools.geometry.courtyard import (
        _courtyard_polygon,
        _fp_transform,
        _side_has_geometry,
    )

    if _side_has_geometry(fp, side):
        poly = _courtyard_polygon(fp, side, Polygon)
        if poly is not None:
            return poly
    if _fp_side(fp) != side:
        return None
    fab = "F.Fab" if side == "F" else "B.Fab"
    transform = _fp_transform(fp)
    pts: list[_Point] = []
    for g in fp.graphics:
        if g.layer != fab:
            continue
        raw = [g.start, g.end, *g.points]
        if g.center is not None and g.radius:
            raw += [
                (g.center[0] - g.radius, g.center[1] - g.radius),
                (g.center[0] + g.radius, g.center[1] + g.radius),
            ]
        pts.extend(transform(p) for p in raw)
    if len(pts) < 2:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if max(xs) - min(xs) <= 0 or max(ys) - min(ys) <= 0:
        return None
    return box(min(xs), min(ys), max(xs), max(ys))


def run_courtyard_overlaps(
    points: list[_Point],
    side: str,
    footprints: Any,
    inflate: float,
    body: Any | None = None,
) -> list[tuple[Footprint, float]]:
    """Same-side bodies a run's envelope overlaps: ``[(footprint, overlap_mm), ...]``.

    The single run-vs-courtyard test shared by :class:`ReinforcementCourtyardRule`
    and ``kct pcb reinforce`` (Issue #8821), so the generator can never plan a
    run the check then rejects. ``inflate`` is wire radius + :data:`RUN_CLEARANCE_MM`;
    ``body(fp, side)`` optionally supplies a cached polygon lookup.
    """
    from shapely.geometry import LineString, Polygon, box  # type: ignore[import-untyped]

    if body is None:

        def body(fp: Footprint, s: str) -> Any | None:
            return _body_polygon(fp, s, Polygon, box)

    line = LineString(points)
    found: list[tuple[Footprint, float]] = []
    for fp in footprints:
        if _fp_side(fp) != side:
            continue
        poly = body(fp, side)
        if poly is None:
            continue
        zone = poly.buffer(inflate)
        if line.intersects(zone):
            found.append((fp, zone.intersection(line).length))
    return found


def _first_point(geom: Any) -> _Point:
    """First coordinate of a (multi)line intersection: a point on the run."""
    if hasattr(geom, "geoms"):
        geom = next(iter(geom.geoms))
    c = geom.coords[0] if hasattr(geom, "coords") and len(geom.coords) else (0.0, 0.0)
    return (float(c[0]), float(c[1]))


def _nearest_on_line(line: Any, p: _Point) -> _Point:
    from shapely.geometry import Point

    q = line.interpolate(line.project(Point(p)))
    return (float(q.x), float(q.y))


class ReinforcementCourtyardRule(ReinforcementIncompleteRule):
    """Flag wire runs and end holes that collide with component bodies (#8820).

    Attributes:
        rule_id: ``"wire_run_courtyard_overlap"``
    """

    rule_id = "wire_run_courtyard_overlap"
    name = "Wire Run Courtyard Overlap"
    description = (
        "Verify reinforced wire runs and their end holes do not pass under "
        "component bodies (courtyards) that make the wire impossible to lay"
    )

    def _severity(self, net: str) -> str:
        return "error"

    # ------------------------------------------------------------------ API

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        results = DRCResults()
        if not self.specs:
            return results

        from kicad_tools.pcb import reinforce as rf
        from kicad_tools.router.current_paths import reinforcement_path_resolutions

        eligible = reinforcement_path_resolutions(pcb, self.specs).eligible
        if not eligible:
            return results

        origin = getattr(pcb, "board_origin", None) or (0.0, 0.0)
        self._ox, self._oy = origin[0], origin[1]

        by_net: dict[str, list] = {}
        for res in eligible:
            by_net.setdefault(res.spec.net_name, []).append(res)
        for net in sorted(by_net):
            self._check_net_bodies(pcb, rf, net, by_net[net], results)
        return results

    # ------------------------------------------------------------- per net

    def _check_net_bodies(
        self, pcb: PCB, rf: Any, net: str, resolutions: list, results: DRCResults
    ) -> None:
        from shapely.geometry import LineString, Point, Polygon, box

        from kicad_tools.core.geometry import rotate_pad_offset
        from kicad_tools.physics.wire_gauge import anchor_drill_for_awg, bare_copper_diameter_mm

        seen: set[int] = set()
        by_layer: dict[str, list] = {}
        for res in resolutions:
            for seg in res.segments:
                if id(seg) in seen or rf._seg_length(seg) <= 1e-9:
                    continue
                seen.add(id(seg))
                by_layer.setdefault(seg.layer, []).append(seg)
        net_number = next((s.net_number for segs in by_layer.values() for s in segs), None)
        if net_number is None:
            return

        change_vias: list[_Point] = [
            v[0] for res in resolutions for ch in res.layer_changes for v in ch.vias
        ]

        def is_change_via(p: _Point) -> bool:
            return any(math.dist(p, q) < 0.05 for q in change_vias)

        legs: list[_Leg] = []
        original_ids: dict[int, int] = {}
        for layer in sorted(by_layer):
            for cand in rf._derive_legs(by_layer[layer], layer, original_ids):
                if cand.status is not None:
                    continue
                pts = rf._run_points(cand.run)
                if len(pts) < 2:
                    continue
                width = max((s.width for s in cand.run), default=0.0)
                legs.append(_Leg(net, layer, pts, width, self._gauge(net, layer, pts)))

        holes = self._anchor_holes(pcb, net_number, rotate_pad_offset)

        # Body polygons per (footprint, side), built lazily.
        cache: dict[tuple[int, str], Any] = {}

        def body(fp: Footprint, side: str) -> Any | None:
            key = (id(fp), side)
            if key not in cache:
                cache[key] = _body_polygon(fp, side, Polygon, box)
            return cache[key]

        reported_holes: set[tuple[int, int, str]] = set()
        for leg in legs:
            side = _side_of_layer(leg.layer)
            wire_r = bare_copper_diameter_mm(leg.gauge) / 2.0
            drill = anchor_drill_for_awg(leg.gauge)
            tol = leg.width / 2.0 + ANCHOR_ON_LEG_SLACK_MM
            line = LineString(leg.points)
            desc = f"run {self._fmt(leg.points[0])} -> {self._fmt(leg.points[-1])}"

            # ---- run vs same-side bodies / opposite-side THT pins ----------
            if side is not None:
                inflate = wire_r + RUN_CLEARANCE_MM
                for fp, length in run_courtyard_overlaps(
                    leg.points, side, pcb.footprints, inflate, body
                ):
                    poly = body(fp, side)
                    hit = line.intersection(poly.buffer(inflate)) if poly is not None else None
                    where = (
                        _first_point(hit) if hit is not None and not hit.is_empty else leg.points[0]
                    )
                    self._emit_run(results, net, leg.layer, desc, fp, side, length, where=where)
                for fp in pcb.footprints:
                    if _fp_side(fp) != side:
                        for pad in fp.pads:
                            if pad.type != "thru_hole" or pad.net_number == net_number:
                                continue
                            ppos = self._pad_pos(fp, pad, rotate_pad_offset)
                            pr = self._pad_radius(pad)
                            if line.distance(Point(ppos)) < inflate + pr + HOLE_FILLET_MM:
                                self._emit_run(
                                    results,
                                    net,
                                    leg.layer,
                                    desc,
                                    fp,
                                    side,
                                    0.0,
                                    pin=pad.number,
                                    where=_nearest_on_line(line, ppos),
                                )
                                break

            # ---- end holes vs courtyards on both sides ---------------------
            for pos, radius, owner in self._leg_holes(
                holes, leg, drill, tol, is_change_via, rotate_pad_offset
            ):
                env = Point(pos).buffer(radius + HOLE_FILLET_MM)
                for fp in pcb.footprints:
                    if self._terminates_in(fp, net_number, pos, rotate_pad_offset) or fp is owner:
                        continue
                    for hside in ("F", "B"):
                        poly = body(fp, hside)
                        if poly is None or not env.intersects(poly):
                            continue
                        key = (round(pos[0] * 1000), round(pos[1] * 1000), f"{fp.reference}{hside}")
                        if key in reported_holes:
                            continue
                        reported_holes.add(key)
                        self._emit_hole(results, net, leg.layer, pos, fp, hside, side)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _pad_pos(fp: Footprint, pad: Any, rotate_pad_offset: Any) -> _Point:
        off = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
        return (fp.position[0] + off[0], fp.position[1] + off[1])

    @staticmethod
    def _pad_radius(pad: Any) -> float:
        try:
            return max(float(pad.size[0]), float(pad.size[1])) / 2.0
        except (TypeError, ValueError, IndexError):
            return 0.0

    def _terminates_in(
        self, fp: Footprint, net_number: int, pos: _Point, rotate_pad_offset: Any
    ) -> bool:
        """True when ``pos`` lies inside a same-net pad of ``fp``."""
        for pad in fp.pads:
            if pad.net_number != net_number:
                continue
            if math.dist(pos, self._pad_pos(fp, pad, rotate_pad_offset)) <= self._pad_radius(pad):
                return True
        return False

    def _leg_holes(
        self,
        holes: dict[str, list],
        leg: _Leg,
        drill: float,
        tol: float,
        is_change_via: Any,
        rotate_pad_offset: Any,
    ) -> list[tuple[_Point, float, Any]]:
        """Anchor-drill holes lying on ``leg``: ``(pos, radius, owner footprint)``."""
        found: list[tuple[_Point, float, Any]] = []
        for via, pos in holes["vias"]:
            if abs(via.drill - drill) > DRILL_TOLERANCE_MM or is_change_via(pos):
                continue
            if via.layers and leg.layer not in via.layers and "*.Cu" not in via.layers:
                continue
            if self._dist_to_polyline(leg.points, pos) <= tol:
                found.append((pos, via.size / 2.0, None))
        for pad, pos, radius in holes["pads"]:
            if abs(pad.drill - drill) > DRILL_TOLERANCE_MM:
                continue
            if min(math.dist(pos, leg.points[0]), math.dist(pos, leg.points[-1])) <= radius + 0.05:
                continue
            if self._dist_to_polyline(leg.points, pos) <= tol:
                found.append((pos, radius, None))
        return found

    def _emit_run(
        self,
        results: DRCResults,
        net: str,
        layer: str,
        desc: str,
        fp: Footprint,
        side: str,
        length: float,
        pin: str | None = None,
        where: _Point = (0.0, 0.0),
    ) -> None:
        side_name = "front" if side == "F" else "back"
        what = (
            f"passes within the pin clearance of foreign-net pin {pin} of {fp.reference} "
            f"({side_name} side, through-hole part on the opposite side)"
            if pin is not None
            else f"overlaps the {side_name}-side courtyard of {fp.reference} by {length:.2f} mm "
            "(wire radius + soldering clearance included)"
        )
        results.add(
            DRCViolation(
                rule_id=self.rule_id,
                severity="error",
                message=f"Wire run courtyard overlap [run] net {net!r} on {layer}, {desc}: {what}",
                location=where,
                layer=layer,
                actual_value=length if pin is None else None,
                items=(net, f"run@{desc}", fp.reference),
                nets=(net,),
            )
        )

    def _emit_hole(
        self,
        results: DRCResults,
        net: str,
        layer: str,
        pos: _Point,
        fp: Footprint,
        hole_side: str,
        run_side: str | None,
    ) -> None:
        where = "front" if hole_side == "F" else "back"
        results.add(
            DRCViolation(
                rule_id=self.rule_id,
                severity="error",
                message=(
                    f"Wire run courtyard overlap [end hole] net {net!r} on {layer}, "
                    f"end hole at {self._fmt(pos)}: hole (pad annulus + fillet clearance) "
                    f"intersects the {where}-side courtyard of {fp.reference}"
                    + (
                        " (opposite the run: the wire tail and fillet exit there)"
                        if run_side is not None and hole_side != run_side
                        else ""
                    )
                ),
                location=pos,
                layer=layer,
                items=(net, f"end_hole@{self._fmt(pos)}", fp.reference, hole_side),
                nets=(net,),
            )
        )
